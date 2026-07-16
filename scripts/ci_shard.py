#!/usr/bin/env python3
"""scripts/ci_shard.py -- Deterministic test-file sharding for CI (P2.3).

What: prints a space-separated list of test file paths for one shard of an
N-way pytest matrix split. Splitting happens on FILES, not individual test
cases (per-test timing data, a la pytest-split, would need a committed
duration cache to stay deterministic across runs/hardware -- a plain sorted
round-robin needs none): `sorted(files)[shard::total]`. That is fully
deterministic -- identical inputs always produce identical output, with no
dependence on filesystem enumeration order, wall-clock timing, or which
worker happens to pick up which test -- and keeps shard file COUNTS balanced
to within +/-1 of each other for any `total`.

Called by: .github/workflows/ci.yml `test` job's matrix, e.g.:
    pytest $(python scripts/ci_shard.py ${{ matrix.shard }} 2)
Depends on: nothing but the stdlib (`argparse`, `pathlib`).

Usage:
    python scripts/ci_shard.py SHARD TOTAL [ROOT]
    # SHARD is 0-indexed (0..TOTAL-1); ROOT defaults to "tests"

Excludes the same paths pytest.ini's `addopts` ignores (tests/e2e, plus
__pycache__/.claude noise) so a shard's file list never includes something
the suite wouldn't collect anyway.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Directory NAMES (not full paths) to exclude anywhere under root, mirroring
# pytest.ini's `--ignore=tests/e2e` / `--ignore=.claude`.
EXCLUDED_DIR_NAMES = {"e2e", "__pycache__", ".claude"}


def list_test_files(root: Path) -> list[Path]:
    """Return a sorted, deterministic list of test_*.py files under `root`."""
    files = []
    for path in root.rglob("test_*.py"):
        parts = path.relative_to(root).parts[:-1]  # directory components only
        if EXCLUDED_DIR_NAMES & set(parts):
            continue
        files.append(path)
    return sorted(files)


def shard_files(files: list[Path], shard: int, total: int) -> list[Path]:
    """Round-robin slice: shard `shard` of `total` gets files[shard::total]."""
    if total < 1:
        raise ValueError(f"total must be >= 1, got {total}")
    if not 0 <= shard < total:
        raise ValueError(f"shard must be in [0, {total}), got {shard}")
    return files[shard::total]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("shard", type=int, help="0-indexed shard number")
    parser.add_argument("total", type=int, help="total number of shards")
    parser.add_argument("root", nargs="?", default="tests", help="test root directory (default: tests)")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"::error::root {root} is not a directory", file=sys.stderr)
        return 2

    all_files = list_test_files(root)
    my_files = shard_files(all_files, args.shard, args.total)
    print(" ".join(str(f) for f in my_files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
