#!/usr/bin/env python3
"""Audit & fix data integrity issues in the AvailAI database.

Run read-only (default):
    PYTHONPATH=/root/availai python scripts/audit_data_integrity.py

Run with fixes:
    PYTHONPATH=/root/availai python scripts/audit_data_integrity.py --fix

After fixing, validates all CHECK constraints added by startup.py.
"""

import argparse
import json
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from app.config import settings
from app.utils.normalization import (
    normalize_condition,
    normalize_packaging,
)

# Valid enum values (must match CHECK constraints in startup.py)
VALID_CONDITIONS = {"new", "refurb", "used"}
VALID_PACKAGINGS = {"reel", "tube", "tray", "bulk", "cut_tape"}
VALID_OFFER_STATUSES = {"active", "expired", "won", "lost", "pending_review"}


def get_engine():
    url = settings.DATABASE_URL
    if not url:
        print("ERROR: DATABASE_URL not configured")
        sys.exit(1)
    return create_engine(url)


def audit(engine, fix: bool = False) -> dict:
    """Run all checks and optionally fix issues. Returns a report dict."""
    report = {"timestamp": datetime.now(timezone.utc).isoformat(), "checks": [], "fixed": 0}

    with engine.connect() as conn:
        # ── 1. Requirements: non-normalized condition ──
        rows = conn.execute(text(
            "SELECT id, condition FROM requirements "
            "WHERE condition IS NOT NULL AND condition NOT IN ('new','refurb','used')"
        )).fetchall()
        report["checks"].append({"name": "req_bad_condition", "count": len(rows)})
        if fix and rows:
            fixed = 0
            for r in rows:
                norm = normalize_condition(r[1])
                if norm:
                    conn.execute(text("UPDATE requirements SET condition = :v WHERE id = :id"), {"v": norm, "id": r[0]})
                    fixed += 1
            conn.commit()
            report["fixed"] += fixed
            print(f"  Fixed {fixed}/{len(rows)} requirement conditions")

        # ── 2. Requirements: non-normalized packaging ──
        rows = conn.execute(text(
            "SELECT id, packaging FROM requirements "
            "WHERE packaging IS NOT NULL AND packaging NOT IN ('reel','tube','tray','bulk','cut_tape')"
        )).fetchall()
        report["checks"].append({"name": "req_bad_packaging", "count": len(rows)})
        if fix and rows:
            fixed = 0
            for r in rows:
                norm = normalize_packaging(r[1])
                if norm:
                    conn.execute(text("UPDATE requirements SET packaging = :v WHERE id = :id"), {"v": norm, "id": r[0]})
                    fixed += 1
            conn.commit()
            report["fixed"] += fixed
            print(f"  Fixed {fixed}/{len(rows)} requirement packagings")

        # ── 3. Requirements: target_qty < 1 ──
        rows = conn.execute(text(
            "SELECT id, target_qty FROM requirements WHERE target_qty IS NOT NULL AND target_qty < 1"
        )).fetchall()
        report["checks"].append({"name": "req_bad_target_qty", "count": len(rows)})
        if fix and rows:
            conn.execute(text("UPDATE requirements SET target_qty = 1 WHERE target_qty IS NOT NULL AND target_qty < 1"))
            conn.commit()
            report["fixed"] += len(rows)
            print(f"  Fixed {len(rows)} requirement target_qty values (set to 1)")

        # ── 4. Requirements: target_price < 0 ──
        rows = conn.execute(text(
            "SELECT id, target_price FROM requirements WHERE target_price IS NOT NULL AND target_price < 0"
        )).fetchall()
        report["checks"].append({"name": "req_bad_target_price", "count": len(rows)})
        if fix and rows:
            conn.execute(text("UPDATE requirements SET target_price = NULL WHERE target_price IS NOT NULL AND target_price < 0"))
            conn.commit()
            report["fixed"] += len(rows)
            print(f"  Fixed {len(rows)} requirement target_price values (set to NULL)")

        # ── 5. Sightings: non-normalized condition ──
        rows = conn.execute(text(
            "SELECT id, condition FROM sightings "
            "WHERE condition IS NOT NULL AND condition NOT IN ('new','refurb','used')"
        )).fetchall()
        report["checks"].append({"name": "sight_bad_condition", "count": len(rows)})
        if fix and rows:
            fixed = 0
            for r in rows:
                norm = normalize_condition(r[1])
                if norm:
                    conn.execute(text("UPDATE sightings SET condition = :v WHERE id = :id"), {"v": norm, "id": r[0]})
                    fixed += 1
                else:
                    conn.execute(text("UPDATE sightings SET condition = NULL WHERE id = :id"), {"id": r[0]})
                    fixed += 1
            conn.commit()
            report["fixed"] += fixed
            print(f"  Fixed {fixed}/{len(rows)} sighting conditions")

        # ── 6. Sightings: non-normalized packaging ──
        rows = conn.execute(text(
            "SELECT id, packaging FROM sightings "
            "WHERE packaging IS NOT NULL AND packaging NOT IN ('reel','tube','tray','bulk','cut_tape')"
        )).fetchall()
        report["checks"].append({"name": "sight_bad_packaging", "count": len(rows)})
        if fix and rows:
            fixed = 0
            for r in rows:
                norm = normalize_packaging(r[1])
                if norm:
                    conn.execute(text("UPDATE sightings SET packaging = :v WHERE id = :id"), {"v": norm, "id": r[0]})
                    fixed += 1
                else:
                    conn.execute(text("UPDATE sightings SET packaging = NULL WHERE id = :id"), {"id": r[0]})
                    fixed += 1
            conn.commit()
            report["fixed"] += fixed
            print(f"  Fixed {fixed}/{len(rows)} sighting packagings")

        # ── 7. Sightings: qty_available <= 0 or unit_price <= 0 ──
        rows = conn.execute(text(
            "SELECT id FROM sightings WHERE qty_available IS NOT NULL AND qty_available <= 0"
        )).fetchall()
        report["checks"].append({"name": "sight_bad_qty", "count": len(rows)})
        if fix and rows:
            conn.execute(text("UPDATE sightings SET qty_available = NULL WHERE qty_available IS NOT NULL AND qty_available <= 0"))
            conn.commit()
            report["fixed"] += len(rows)
            print(f"  Fixed {len(rows)} sighting qty_available values (set to NULL)")

        rows = conn.execute(text(
            "SELECT id FROM sightings WHERE unit_price IS NOT NULL AND unit_price <= 0"
        )).fetchall()
        report["checks"].append({"name": "sight_bad_price", "count": len(rows)})
        if fix and rows:
            conn.execute(text("UPDATE sightings SET unit_price = NULL WHERE unit_price IS NOT NULL AND unit_price <= 0"))
            conn.commit()
            report["fixed"] += len(rows)
            print(f"  Fixed {len(rows)} sighting unit_price values (set to NULL)")

        # ── 8. Sightings: confidence outside 0-1 ──
        rows = conn.execute(text(
            "SELECT id FROM sightings WHERE confidence IS NOT NULL AND (confidence < 0 OR confidence > 1)"
        )).fetchall()
        report["checks"].append({"name": "sight_bad_confidence", "count": len(rows)})
        if fix and rows:
            conn.execute(text(
                "UPDATE sightings SET confidence = LEAST(GREATEST(confidence, 0), 1) "
                "WHERE confidence IS NOT NULL AND (confidence < 0 OR confidence > 1)"
            ))
            conn.commit()
            report["fixed"] += len(rows)
            print(f"  Fixed {len(rows)} sighting confidence values (clamped to 0-1)")

        # ── 9. Sightings: negative lead_time_days ──
        rows = conn.execute(text(
            "SELECT id FROM sightings WHERE lead_time_days IS NOT NULL AND lead_time_days < 0"
        )).fetchall()
        report["checks"].append({"name": "sight_bad_lead_time", "count": len(rows)})
        if fix and rows:
            conn.execute(text("UPDATE sightings SET lead_time_days = NULL WHERE lead_time_days IS NOT NULL AND lead_time_days < 0"))
            conn.commit()
            report["fixed"] += len(rows)
            print(f"  Fixed {len(rows)} sighting lead_time_days values (set to NULL)")

        # ── 10. Offers: non-normalized condition ──
        rows = conn.execute(text(
            "SELECT id, condition FROM offers "
            "WHERE condition IS NOT NULL AND condition NOT IN ('new','refurb','used')"
        )).fetchall()
        report["checks"].append({"name": "offer_bad_condition", "count": len(rows)})
        if fix and rows:
            fixed = 0
            for r in rows:
                norm = normalize_condition(r[1])
                if norm:
                    conn.execute(text("UPDATE offers SET condition = :v WHERE id = :id"), {"v": norm, "id": r[0]})
                    fixed += 1
                else:
                    conn.execute(text("UPDATE offers SET condition = NULL WHERE id = :id"), {"id": r[0]})
                    fixed += 1
            conn.commit()
            report["fixed"] += fixed
            print(f"  Fixed {fixed}/{len(rows)} offer conditions")

        # ── 11. Offers: non-normalized packaging ──
        rows = conn.execute(text(
            "SELECT id, packaging FROM offers "
            "WHERE packaging IS NOT NULL AND packaging NOT IN ('reel','tube','tray','bulk','cut_tape')"
        )).fetchall()
        report["checks"].append({"name": "offer_bad_packaging", "count": len(rows)})
        if fix and rows:
            fixed = 0
            for r in rows:
                norm = normalize_packaging(r[1])
                if norm:
                    conn.execute(text("UPDATE offers SET packaging = :v WHERE id = :id"), {"v": norm, "id": r[0]})
                    fixed += 1
                else:
                    conn.execute(text("UPDATE offers SET packaging = NULL WHERE id = :id"), {"id": r[0]})
                    fixed += 1
            conn.commit()
            report["fixed"] += fixed
            print(f"  Fixed {fixed}/{len(rows)} offer packagings")

        # ── 12. Offers: invalid status ──
        rows = conn.execute(text(
            "SELECT id, status FROM offers "
            "WHERE status NOT IN ('active','expired','won','lost','pending_review')"
        )).fetchall()
        report["checks"].append({"name": "offer_bad_status", "count": len(rows)})
        if fix and rows:
            conn.execute(text(
                "UPDATE offers SET status = 'active' "
                "WHERE status NOT IN ('active','expired','won','lost','pending_review')"
            ))
            conn.commit()
            report["fixed"] += len(rows)
            print(f"  Fixed {len(rows)} offer status values (set to 'active')")

        # ── Validate constraints if we fixed anything ──
        if fix and report["fixed"] > 0:
            print("\nValidating CHECK constraints...")
            constraint_names = [
                "chk_req_target_qty", "chk_req_target_price", "chk_req_condition", "chk_req_packaging",
                "chk_sight_qty", "chk_sight_price", "chk_sight_moq", "chk_sight_confidence",
                "chk_sight_score", "chk_sight_lead_time", "chk_sight_condition", "chk_sight_packaging",
                "chk_offer_qty", "chk_offer_price", "chk_offer_moq",
                "chk_offer_condition", "chk_offer_packaging", "chk_offer_status",
            ]
            table_map = {
                "chk_req_": "requirements",
                "chk_sight_": "sightings",
                "chk_offer_": "offers",
            }
            for cname in constraint_names:
                tbl = next((t for prefix, t in table_map.items() if cname.startswith(prefix)), None)
                if not tbl:
                    continue
                try:
                    conn.execute(text(f"ALTER TABLE {tbl} VALIDATE CONSTRAINT {cname}"))
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    print(f"  WARNING: Could not validate {cname}: {e}")

    return report


def main():
    parser = argparse.ArgumentParser(description="Audit & fix AvailAI data integrity")
    parser.add_argument("--fix", action="store_true", help="Apply fixes to non-conforming data")
    parser.add_argument("--json", action="store_true", help="Output report as JSON")
    args = parser.parse_args()

    engine = get_engine()
    print(f"{'AUDIT + FIX' if args.fix else 'AUDIT (read-only)'} — {datetime.now(timezone.utc).isoformat()}\n")

    report = audit(engine, fix=args.fix)

    # Summary
    print("\n── Summary ──")
    total_issues = 0
    for check in report["checks"]:
        status = "OK" if check["count"] == 0 else f"{check['count']} issues"
        print(f"  {check['name']:30s} {status}")
        total_issues += check["count"]

    print(f"\n  Total issues found: {total_issues}")
    if args.fix:
        print(f"  Total rows fixed:   {report['fixed']}")

    if args.json:
        print("\n" + json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
