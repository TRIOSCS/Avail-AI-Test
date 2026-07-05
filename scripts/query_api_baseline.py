"""scripts/query_api_baseline.py — Legacy SQLAlchemy Query-API ratchet counter.

Counts occurrences of the legacy SQLAlchemy 1.x ``Query`` API — i.e. any
``<session>.query(`` call (``db.query(``, ``session.query(``, ``self.db.query(``,
``write_db.query(`` …) — across ``app/**/*.py``. This is the single source of
truth for both the committed baseline (``tests/query_api_baseline.json``) and the
ratchet test (``tests/test_query_api_ratchet.py``), so the count is always
self-consistent: whatever the counter counts is exactly what the baseline
records and what the test re-checks.

The match is a deliberately simple TEXTUAL count (regex ``\\w\\.query\\s*\\(``).
It does NOT skip matches inside comments or strings — that is an accepted
NICE-to-have, not a requirement. Because the baseline is produced by this very
function, occasional in-comment/in-string matches are counted identically on
both sides and never cause a false ratchet break. Every ``.query(`` in ``app/``
today is a SQLAlchemy Session call (no ElasticSearch/httpx ``.query(`` etc.), so
this pattern has no false positives in the current tree.

The ratchet is DOWN-only: new legacy sites make CURRENT > baseline and fail the
test; removing sites and lowering the baseline is the sanctioned path.

Called by:
    - tests/test_query_api_ratchet.py (imports ``count_legacy_query_calls`` and
      ``load_baseline``).
    - Developers, to refresh the baseline after intentionally removing sites:
      ``python -m scripts.query_api_baseline --write``.
Depends on: stdlib only (re, json, pathlib, argparse).

Usage:
    python -m scripts.query_api_baseline           # print current count
    python -m scripts.query_api_baseline --check    # exit 1 if count grew (hook/CI)
    python -m scripts.query_api_baseline --write    # rewrite the baseline json
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# Repo root = parent of this ``scripts/`` dir. Resolved from __file__ so the
# counter works from any CWD (worktrees, CI, pre-commit).
REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"
BASELINE_PATH = REPO_ROOT / "tests" / "query_api_baseline.json"

# A word char immediately before ``.query(`` anchors the match to a real
# receiver (``db``, ``session``, ``self.db`` …) rather than a bare/stray
# ``.query(`` in a URL-ish string. ``\s*`` tolerates any spacing before ``(``.
LEGACY_QUERY_RE = re.compile(r"\w\.query\s*\(")


def count_legacy_query_calls(app_dir: Path | None = None) -> int:
    """Return the total count of legacy ``<session>.query(`` calls under ``app_dir``
    (defaults to the repo's ``app/``)."""
    root = app_dir if app_dir is not None else APP_DIR
    total = 0
    for py_file in sorted(root.rglob("*.py")):
        text = py_file.read_text(encoding="utf-8", errors="replace")
        total += len(LEGACY_QUERY_RE.findall(text))
    return total


def load_baseline(baseline_path: Path | None = None) -> dict:
    """Load the committed baseline dict (``{"total": <int>, ...}``)."""
    path = baseline_path if baseline_path is not None else BASELINE_PATH
    return json.loads(path.read_text(encoding="utf-8"))


def _write_baseline(total: int, baseline_path: Path | None = None) -> None:
    path = baseline_path if baseline_path is not None else BASELINE_PATH
    payload = {
        "total": total,
        "note": (
            "Legacy SQLAlchemy 1.x Query-API call count under app/. DOWN-only "
            "ratchet enforced by tests/test_query_api_ratchet.py. Regenerate "
            "with: python -m scripts.query_api_baseline --write (only after "
            "intentionally REMOVING sites)."
        ),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Rewrite tests/query_api_baseline.json with the current count.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if the current count exceeds the committed baseline.",
    )
    args = parser.parse_args()

    current = count_legacy_query_calls()

    if args.write:
        _write_baseline(current)
        print(f"Wrote baseline: total={current} -> {BASELINE_PATH}")
        return 0

    if args.check:
        baseline = load_baseline()["total"]
        if current > baseline:
            print(
                f"Legacy Query API count rose from {baseline} to {current} — new "
                f"code must use SQLAlchemy 2.0 style (db.get(), "
                f"db.scalars(select(...)), db.execute(select(...))). If you "
                f"intentionally REMOVED sites, lower tests/query_api_baseline.json "
                f"to match (run: python -m scripts.query_api_baseline --write)."
            )
            return 1
        print(f"OK: legacy Query API count {current} <= baseline {baseline}")
        return 0

    print(current)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
