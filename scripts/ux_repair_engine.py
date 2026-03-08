#!/usr/bin/env python3
"""UX repair engine — in-container CLI for the ultimate repair orchestrator.

Provides JSON-outputting commands for sweep, smoke-test, ticket creation,
patch polling, ticket summary, fix confidence scoring, and Teams notification.

Called by: scripts/ultimate_ux_repair.sh (via docker compose exec)
Depends on: app.services.site_tester, app.services.trouble_ticket_service,
            app.services.teams_notifications, app.models.trouble_ticket,
            app.models.self_heal_log, app.database
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

# Ensure app is importable
sys.path.insert(0, "/app")
os.environ.setdefault("TESTING", "")

from loguru import logger

# Suppress loguru output — all output must be JSON to stdout
logger.remove()
logger.add(sys.stderr, level="WARNING")

BASE_URL = "http://localhost:8000"
ADMIN_USER_ID = 1
FIX_QUEUE_DIR = Path(os.environ.get("FIX_QUEUE_DIR", "/app/fix_queue"))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _output(data: dict | list) -> None:
    """Print JSON to stdout (the only allowed output channel)."""
    print(json.dumps(data, default=str))


def _get_session_cookie() -> str:
    """Create a signed session cookie for admin user."""
    from itsdangerous import URLSafeTimedSerializer
    from app.config import settings

    signer = URLSafeTimedSerializer(settings.secret_key)
    return signer.dumps({"user_id": ADMIN_USER_ID})


def _get_db():
    from app.database import SessionLocal
    return SessionLocal()


# ---------------------------------------------------------------------------
# Command: sweep
# ---------------------------------------------------------------------------

async def cmd_sweep(areas_json: str | None = None, baseline_file: str | None = None) -> None:
    """Run SiteTester sweep, optionally filtered by areas and diffed against baseline."""
    from app.services.site_tester import SiteTester, TEST_AREAS

    cookie = _get_session_cookie()
    tester = SiteTester(base_url=BASE_URL, session_cookie=cookie)

    # Filter areas if provided
    if areas_json:
        requested = json.loads(areas_json)
        original_areas = list(TEST_AREAS)
        TEST_AREAS.clear()
        for area in original_areas:
            if area["name"] in requested:
                TEST_AREAS.append(area)

    try:
        issues = await tester.run_full_sweep()
    except Exception as e:
        _output({"ok": False, "error": str(e), "issues": [], "areas_tested": 0})
        return
    finally:
        # Restore TEST_AREAS if we modified it
        if areas_json:
            from app.services.site_tester import TEST_AREAS as ta
            ta.clear()
            ta.extend(original_areas)

    # Diff against baseline if provided
    new_issues = list(issues)
    if baseline_file and os.path.exists(baseline_file):
        with open(baseline_file) as f:
            baseline = json.load(f)
        baseline_keys = {(b.get("area", ""), b.get("title", "")[:80]) for b in baseline}
        new_issues = [i for i in issues if (i.get("area", ""), i.get("title", "")[:80]) not in baseline_keys]

    _output({
        "ok": True,
        "total_issues": len(issues),
        "new_issues": len(new_issues),
        "areas_tested": len(tester.progress),
        "issues": new_issues,
    })


# ---------------------------------------------------------------------------
# Command: smoke-test
# ---------------------------------------------------------------------------

async def cmd_smoke_test() -> None:
    """Hit key API endpoints with health probes."""
    import httpx

    cookie = _get_session_cookie()
    endpoints = [
        ("GET", "/api/health", None),
        ("GET", "/api/system/alerts", None),
        ("GET", "/api/requisitions?limit=1", None),
        ("GET", "/api/companies?limit=1", None),
        ("GET", "/api/vendor-contacts?limit=1", None),
        ("GET", "/api/dashboard/briefing", None),
    ]

    results = []
    async with httpx.AsyncClient(
        base_url=BASE_URL,
        cookies={"session": cookie},
        timeout=15.0,
    ) as client:
        for method, path, body in endpoints:
            start = time.monotonic()
            try:
                if method == "GET":
                    resp = await client.get(path)
                else:
                    resp = await client.post(path, json=body)
                elapsed_ms = (time.monotonic() - start) * 1000
                results.append({
                    "endpoint": path,
                    "status": resp.status_code,
                    "ok": 200 <= resp.status_code < 400,
                    "ms": round(elapsed_ms, 1),
                })
            except Exception as e:
                elapsed_ms = (time.monotonic() - start) * 1000
                results.append({
                    "endpoint": path,
                    "status": 0,
                    "ok": False,
                    "ms": round(elapsed_ms, 1),
                    "error": str(e),
                })

    all_ok = all(r["ok"] for r in results)
    _output({"ok": all_ok, "results": results})


# ---------------------------------------------------------------------------
# Command: create-tickets
# ---------------------------------------------------------------------------

async def cmd_create_tickets(issues_json: str) -> None:
    """Create trouble tickets from sweep issues and auto-process them."""
    from app.services.site_tester import create_tickets_from_issues
    from app.services.trouble_ticket_service import auto_process_ticket
    from app.models.trouble_ticket import TroubleTicket

    issues = json.loads(issues_json)
    if not issues:
        _output({"ok": True, "created": 0, "ticket_ids": []})
        return

    db = _get_db()
    try:
        count = await create_tickets_from_issues(issues, db)

        # Get the IDs of recently created playwright tickets
        recent = (
            db.query(TroubleTicket)
            .filter(TroubleTicket.source == "playwright")
            .filter(TroubleTicket.status == "submitted")
            .order_by(TroubleTicket.id.desc())
            .limit(count)
            .all()
        )
        ticket_ids = [t.id for t in recent]

        # Auto-process each ticket (diagnose + queue fix)
        processed = 0
        for tid in ticket_ids:
            try:
                await auto_process_ticket(tid)
                processed += 1
            except Exception as e:
                logger.warning("Auto-process failed for ticket #{}: {}", tid, e)

        _output({
            "ok": True,
            "created": count,
            "processed": processed,
            "ticket_ids": ticket_ids,
        })
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Command: poll-queue
# ---------------------------------------------------------------------------

async def cmd_poll_queue(ticket_ids_json: str, timeout_secs: int = 120) -> None:
    """Poll fix_queue/ until patches appear for given tickets or timeout."""
    ticket_ids = json.loads(ticket_ids_json)
    if not ticket_ids:
        _output({"ok": True, "found": [], "missing": []})
        return

    deadline = time.monotonic() + timeout_secs
    found: dict[int, str] = {}
    skipped: list[int] = []

    while time.monotonic() < deadline:
        skipped = []
        for tid in ticket_ids:
            if tid in found:
                continue
            # Look for patch files matching this ticket ID
            pattern = f"ticket_{tid}_*.py"
            matches = list(FIX_QUEUE_DIR.glob(pattern))
            if not matches:
                # Also try JSON patch format
                pattern_json = f"ticket_{tid}_*.json"
                matches = list(FIX_QUEUE_DIR.glob(pattern_json))
            if matches:
                found[tid] = str(matches[0])
            else:
                skipped.append(tid)

        if len(found) == len(ticket_ids):
            break

        await asyncio.sleep(3)

    _output({
        "ok": len(skipped) == 0,
        "found": [{"ticket_id": tid, "file": path} for tid, path in found.items()],
        "missing": skipped,
        "timeout_reached": time.monotonic() >= deadline,
    })


# ---------------------------------------------------------------------------
# Command: ticket-summary
# ---------------------------------------------------------------------------

async def cmd_ticket_summary() -> None:
    """Get ticket status counts and total AI cost."""
    from sqlalchemy import func
    from app.models.trouble_ticket import TroubleTicket
    from app.models.self_heal_log import SelfHealLog

    db = _get_db()
    try:
        # Status counts
        status_rows = (
            db.query(TroubleTicket.status, func.count())
            .group_by(TroubleTicket.status)
            .all()
        )
        statuses = {status: count for status, count in status_rows}

        # Total AI cost from self_heal_log
        total_cost = db.query(func.coalesce(func.sum(SelfHealLog.cost_usd), 0.0)).scalar()

        # Total ticket count
        total = sum(statuses.values())

        _output({
            "ok": True,
            "total": total,
            "statuses": statuses,
            "total_ai_cost_usd": round(float(total_cost), 4),
        })
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Command: fix-confidence
# ---------------------------------------------------------------------------

async def cmd_fix_confidence(fix_file: str) -> None:
    """Score a patch file by search-string match quality.

    1.0 = unique match in target file
    0.5 = ambiguous (multiple matches)
    0.0 = search string not found
    """
    fix_path = Path(fix_file)
    if not fix_path.exists():
        _output({"ok": False, "error": f"File not found: {fix_file}", "score": 0.0})
        return

    content = fix_path.read_text()

    # Try to parse as JSON patch format
    try:
        patches = json.loads(content)
        if isinstance(patches, dict):
            patches = [patches]
    except json.JSONDecodeError:
        # Not JSON — treat as a Python patch script, score 0.5 (can't verify)
        _output({"ok": True, "score": 0.5, "reason": "non-json patch format"})
        return

    scores = []
    details = []

    for patch in patches:
        file_path = patch.get("file", patch.get("file_path", ""))
        search = patch.get("search", patch.get("old_string", ""))

        if not file_path or not search:
            scores.append(0.0)
            details.append({"file": file_path, "score": 0.0, "reason": "missing file or search string"})
            continue

        target = Path(file_path)
        if not target.exists():
            # Try with /app prefix
            target = Path("/app") / file_path.lstrip("/")

        if not target.exists():
            scores.append(0.0)
            details.append({"file": file_path, "score": 0.0, "reason": "target file not found"})
            continue

        target_content = target.read_text()
        match_count = target_content.count(search)

        if match_count == 1:
            scores.append(1.0)
            details.append({"file": file_path, "score": 1.0, "reason": "unique match"})
        elif match_count > 1:
            scores.append(0.5)
            details.append({"file": file_path, "score": 0.5, "reason": f"ambiguous ({match_count} matches)"})
        else:
            scores.append(0.0)
            details.append({"file": file_path, "score": 0.0, "reason": "search string not found"})

    avg_score = sum(scores) / len(scores) if scores else 0.0

    _output({
        "ok": True,
        "score": round(avg_score, 2),
        "patch_count": len(patches),
        "details": details,
    })


# ---------------------------------------------------------------------------
# Command: teams-notify
# ---------------------------------------------------------------------------

async def cmd_teams_notify(message: str) -> None:
    """Send a Teams channel notification."""
    from app.services.teams_notifications import post_teams_channel

    try:
        await post_teams_channel(message)
        _output({"ok": True})
    except Exception as e:
        _output({"ok": False, "error": str(e)})


# ---------------------------------------------------------------------------
# CLI dispatcher
# ---------------------------------------------------------------------------

async def main() -> None:
    if len(sys.argv) < 2:
        _output({"ok": False, "error": "Usage: ux_repair_engine.py <command> [args...]"})
        sys.exit(1)

    command = sys.argv[1]

    if command == "sweep":
        areas = sys.argv[2] if len(sys.argv) > 2 else None
        baseline = sys.argv[3] if len(sys.argv) > 3 else None
        await cmd_sweep(areas, baseline)

    elif command == "smoke-test":
        await cmd_smoke_test()

    elif command == "create-tickets":
        if len(sys.argv) < 3:
            _output({"ok": False, "error": "create-tickets requires issues_json arg"})
            sys.exit(1)
        await cmd_create_tickets(sys.argv[2])

    elif command == "poll-queue":
        if len(sys.argv) < 3:
            _output({"ok": False, "error": "poll-queue requires ticket_ids_json arg"})
            sys.exit(1)
        timeout = int(sys.argv[3]) if len(sys.argv) > 3 else 120
        await cmd_poll_queue(sys.argv[2], timeout)

    elif command == "ticket-summary":
        await cmd_ticket_summary()

    elif command == "fix-confidence":
        if len(sys.argv) < 3:
            _output({"ok": False, "error": "fix-confidence requires fix_file arg"})
            sys.exit(1)
        await cmd_fix_confidence(sys.argv[2])

    elif command == "teams-notify":
        if len(sys.argv) < 3:
            _output({"ok": False, "error": "teams-notify requires message arg"})
            sys.exit(1)
        await cmd_teams_notify(sys.argv[2])

    else:
        _output({"ok": False, "error": f"Unknown command: {command}"})
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
