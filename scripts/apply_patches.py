#!/usr/bin/env python3
"""Apply patches from a self-heal fix JSON file to the source tree.

Reads a fix JSON file produced by the self-heal pipeline (execution_service.py)
and applies each patch entry by performing exact string replacement in the
target source file.

What calls it:
    scripts/self_heal_watcher.sh — the host-side watcher that monitors
    fix_queue/ for new fix files and orchestrates the full apply-rebuild-retest
    cycle.

What it depends on:
    Python 3 stdlib only (json, sys, os, pathlib). No pip packages required.

Exit codes:
    0 — all patches applied successfully
    1 — one or more patches failed (file not found, search string missing, etc.)
    2 — bad arguments or unreadable JSON
"""

import json
import sys
from pathlib import Path

PROJ_DIR = Path(__file__).resolve().parent.parent


def apply_patch(patch: dict, index: int) -> bool:
    """Apply a single patch entry. Returns True on success."""
    rel_path = patch.get("file", "")
    search = patch.get("search", "")
    replace = patch.get("replace", "")

    if not rel_path or not search:
        print(f"  [{index}] ERR  Missing 'file' or 'search' in patch entry")
        return False

    target = PROJ_DIR / rel_path

    if not target.is_file():
        print(f"  [{index}] ERR  File not found: {rel_path}")
        return False

    try:
        content = target.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"  [{index}] ERR  Cannot read {rel_path}: {exc}")
        return False

    if search not in content:
        print(f"  [{index}] WARN Search string not found in {rel_path}")
        return False

    new_content = content.replace(search, replace, 1)

    try:
        target.write_text(new_content, encoding="utf-8")
    except Exception as exc:
        print(f"  [{index}] ERR  Cannot write {rel_path}: {exc}")
        return False

    print(f"  [{index}] OK   Patched {rel_path}")
    return True


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <fix-json-file>")
        return 2

    fix_path = Path(sys.argv[1])
    if not fix_path.is_file():
        print(f"ERR  Fix file not found: {fix_path}")
        return 2

    try:
        data = json.loads(fix_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"ERR  Cannot parse fix file: {exc}")
        return 2

    patches = data.get("patches", [])
    if not patches:
        print("WARN No patches in fix file")
        return 0

    ticket_id = data.get("ticket_id", "?")
    print(f"Applying {len(patches)} patch(es) for ticket #{ticket_id}")

    all_ok = True
    for i, patch in enumerate(patches):
        if not apply_patch(patch, i):
            all_ok = False

    if all_ok:
        print(f"All {len(patches)} patch(es) applied successfully")
        return 0
    else:
        print("One or more patches failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
