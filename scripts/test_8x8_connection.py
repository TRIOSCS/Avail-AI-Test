"""One-off script to test 8x8 API connectivity.

Authenticates, fetches last 24h of CDRs, and prints a masked sample.
Read-only — does not write to database.

Usage: PYTHONPATH=/root/availai python scripts/test_8x8_connection.py
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/root/availai")

from app.config import settings
from app.services.eight_by_eight_service import get_access_token, get_cdrs, normalize_cdr


def mask_phone(phone: str) -> str:
    """Show only last 4 digits of a phone number."""
    digits = re.sub(r"\D", "", phone)
    if len(digits) <= 4:
        return phone
    return "***" + digits[-4:]


def main():
    print("=" * 50)
    print("8x8 Work Analytics — Connection Test")
    print("=" * 50)

    if not settings.eight_by_eight_api_key:
        print("EIGHT_BY_EIGHT_API_KEY not set in .env")
        return

    print(f"API Key: {settings.eight_by_eight_api_key[:10]}...")
    print(f"PBX ID:  {settings.eight_by_eight_pbx_id[:10]}...")
    print(f"Username: {settings.eight_by_eight_username or '(not set)'}")
    print()

    # Step 1: Auth
    print("[1] Authenticating...")
    try:
        token = get_access_token(settings)
        print(f"    Auth successful — token: {token[:20]}...")
    except ValueError as e:
        print(f"    Auth FAILED: {e}")
        return

    # Step 2: Fetch CDRs
    print()
    print("[2] Fetching CDRs (last 24 hours)...")
    until = datetime.now(timezone.utc)
    since = until - timedelta(hours=24)
    print(f"    Window: {since.strftime('%Y-%m-%d %H:%M')} → {until.strftime('%Y-%m-%d %H:%M')} UTC")

    cdrs = get_cdrs(token, settings, since, until)
    print(f"    Records returned: {len(cdrs)}")

    if not cdrs:
        print("    No records — try a wider time window or check PBX ID")
        return

    # Step 3: Show first record (masked)
    print()
    print("[3] First CDR (raw, masked):")
    first = cdrs[0].copy()
    for key in ("caller", "callee"):
        if key in first:
            first[key] = mask_phone(str(first[key]))
    print(json.dumps(first, indent=2, default=str))

    # Step 4: Normalize it
    print()
    print("[4] Normalized:")
    norm = normalize_cdr(cdrs[0])
    norm["caller_phone"] = mask_phone(norm["caller_phone"])
    norm["callee_phone"] = mask_phone(norm["callee_phone"])
    norm["occurred_at"] = str(norm["occurred_at"])
    print(json.dumps(norm, indent=2))

    # Step 5: Summary stats
    print()
    print("[5] Summary:")
    directions = {}
    missed_count = 0
    extensions = {}
    for cdr in cdrs:
        d = cdr.get("direction", "Unknown")
        directions[d] = directions.get(d, 0) + 1
        if cdr.get("missed") == "Missed":
            missed_count += 1
        # Track extensions and names
        norm = normalize_cdr(cdr)
        ext = norm["extension"]
        name = norm["caller_name"] if d == "Outgoing" else norm["callee_name"]
        if ext and ext not in extensions:
            extensions[ext] = name or "(unknown)"
    for d, count in sorted(directions.items()):
        print(f"    {d}: {count}")
    print(f"    Missed: {missed_count}")
    print()
    print("[6] Extensions found:")
    for ext, name in sorted(extensions.items()):
        print(f"    ext {ext} — {name}")
    print()
    print("Done. No data written to database.")


if __name__ == "__main__":
    main()
