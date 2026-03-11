"""Remediate workflow data issues before UAT release.

Identifies and optionally fixes common data integrity problems:
1. Requisitions with invalid/legacy status values
2. Requirements with missing MPN or invalid target_qty
3. Buy plan header/line status mismatches
4. Orphan buy plan lines (no parent plan)
5. Requirements stuck in sourcing for >48h

Usage:
    python scripts/remediate_workflow_data.py --dry-run   # Preview (default)
    python scripts/remediate_workflow_data.py --fix        # Fix unambiguous issues

Called by: manual execution before UAT
Depends on: app.database, app.models
"""

import argparse
import csv
import sys
from datetime import datetime, timedelta, timezone

from loguru import logger

sys.path.insert(0, "/root/availai")

from app.database import SessionLocal
from app.models import Requirement, Requisition
from app.models.buy_plan import BuyPlanLine, BuyPlanStatus, BuyPlanV3


def check_invalid_requisition_statuses(db, fix=False):
    """Find requisitions with statuses not in the valid enum."""
    valid = {"draft", "active", "open", "sourcing", "offers", "quoting",
             "quoted", "reopened", "won", "lost", "archived"}
    all_reqs = db.query(Requisition).all()
    issues = []
    for r in all_reqs:
        if r.status not in valid:
            issues.append({
                "id": r.id, "name": r.name, "status": r.status,
                "issue": "invalid_status",
            })
            if fix:
                logger.info("Fixing requisition %d: %s → draft", r.id, r.status)
                r.status = "draft"

    if fix and issues:
        db.commit()
    return issues


def check_bad_requirements(db, fix=False):
    """Find requirements with missing MPN or invalid target_qty."""
    issues = []
    bad_reqs = db.query(Requirement).filter(
        (Requirement.primary_mpn.is_(None))
        | (Requirement.primary_mpn == "")
        | (Requirement.target_qty.is_(None))
        | (Requirement.target_qty < 1)
    ).all()
    for r in bad_reqs:
        issue_type = []
        if not r.primary_mpn:
            issue_type.append("missing_mpn")
        if r.target_qty is None or r.target_qty < 1:
            issue_type.append("invalid_qty")
        issues.append({
            "id": r.id, "requisition_id": r.requisition_id,
            "primary_mpn": r.primary_mpn, "target_qty": r.target_qty,
            "issue": ", ".join(issue_type),
        })
        if fix and r.target_qty is not None and r.target_qty < 1:
            logger.info("Fixing requirement %d: qty %d → 1", r.id, r.target_qty)
            r.target_qty = 1

    if fix and issues:
        db.commit()
    return issues


def check_buyplan_status_mismatches(db, fix=False):
    """Find buy plans where header status doesn't match line states."""
    issues = []
    plans = db.query(BuyPlanV3).all()
    for plan in plans:
        lines = db.query(BuyPlanLine).filter_by(buy_plan_id=plan.id).all()
        if not lines:
            if plan.status == BuyPlanStatus.active.value:
                issues.append({
                    "plan_id": plan.id, "status": plan.status,
                    "issue": "active_plan_no_lines",
                })
            continue

        terminal = {"verified", "cancelled"}
        all_terminal = all(l.status in terminal for l in lines)
        all_cancelled = all(l.status == "cancelled" for l in lines)

        if plan.status == BuyPlanStatus.active.value and all_cancelled:
            issues.append({
                "plan_id": plan.id, "status": plan.status,
                "line_count": len(lines),
                "issue": "active_plan_all_lines_cancelled",
            })

        if plan.status == BuyPlanStatus.completed.value and not all_terminal:
            non_terminal = [l for l in lines if l.status not in terminal]
            issues.append({
                "plan_id": plan.id, "status": plan.status,
                "non_terminal_lines": len(non_terminal),
                "issue": "completed_plan_non_terminal_lines",
            })

    return issues


def check_orphan_buyplan_lines(db, fix=False):
    """Find buy plan lines whose parent plan doesn't exist."""
    from sqlalchemy import not_, select

    plan_ids = select(BuyPlanV3.id)
    orphans = db.query(BuyPlanLine).filter(
        not_(BuyPlanLine.buy_plan_id.in_(plan_ids))
    ).all()
    issues = [{"line_id": l.id, "buy_plan_id": l.buy_plan_id, "issue": "orphan_line"}
              for l in orphans]
    return issues


def check_stuck_requirements(db, fix=False):
    """Find requirements stuck in 'sourcing' for >48h."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    stuck = db.query(Requirement).filter(
        Requirement.sourcing_status == "sourcing",
        Requirement.updated_at < cutoff,
    ).all()
    issues = []
    for r in stuck:
        issues.append({
            "id": r.id, "requisition_id": r.requisition_id,
            "primary_mpn": r.primary_mpn,
            "sourcing_status": r.sourcing_status,
            "updated_at": str(r.updated_at),
            "issue": "stuck_sourcing_48h",
        })
        if fix:
            logger.info("Fixing requirement %d: sourcing → open (stuck)", r.id)
            r.sourcing_status = "open"

    if fix and issues:
        db.commit()
    return issues


def main():
    parser = argparse.ArgumentParser(description="Remediate workflow data issues")
    parser.add_argument("--fix", action="store_true", help="Fix unambiguous issues (default: dry-run)")
    parser.add_argument("--csv-out", type=str, help="Write issues to CSV file")
    args = parser.parse_args()

    dry_run = not args.fix
    mode = "DRY RUN" if dry_run else "FIX MODE"
    logger.info("=== Workflow Data Remediation (%s) ===", mode)

    db = SessionLocal()
    all_issues = []

    checks = [
        ("Invalid Requisition Statuses", check_invalid_requisition_statuses),
        ("Bad Requirements (MPN/Qty)", check_bad_requirements),
        ("Buy Plan Status Mismatches", check_buyplan_status_mismatches),
        ("Orphan Buy Plan Lines", check_orphan_buyplan_lines),
        ("Stuck Requirements (>48h)", check_stuck_requirements),
    ]

    for name, check_fn in checks:
        issues = check_fn(db, fix=args.fix)
        logger.info("%s: %d issues found", name, len(issues))
        for issue in issues:
            issue["check"] = name
            all_issues.append(issue)

    if args.csv_out and all_issues:
        keys = set()
        for issue in all_issues:
            keys.update(issue.keys())
        with open(args.csv_out, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=sorted(keys))
            writer.writeheader()
            writer.writerows(all_issues)
        logger.info("Wrote %d issues to %s", len(all_issues), args.csv_out)

    total = len(all_issues)
    logger.info("=== Total: %d issues ===", total)
    if dry_run and total > 0:
        logger.info("Re-run with --fix to auto-fix unambiguous issues")

    db.close()
    return total


if __name__ == "__main__":
    sys.exit(0 if main() == 0 else 1)
