#!/usr/bin/env python3
"""scripts/lint_assertion_theater.py -- Flags "assertion-theater" tests (P2.4).

What: stdlib-`ast`-only lint that flags any test function whose ONLY
assertion is a bare `assert <expr>.status_code == 200` or `assert x is not
None` -- the audit-identified pattern (docs/CODE_AUDIT_AND_HARDENING_PLAN.md
P2.4, backfill tracked as P6.1) where a test exercises a code path but never
asserts on its actual output, so a broken response body/template can ship
silently while the test suite stays green.

Called by: .github/workflows/ci.yml's "Assertion-theater lint" step, scoped
to files CHANGED vs the PR base (same merge-base pattern as the pre-commit
step in that workflow) -- so the ~542 pre-existing offenders never block an
unrelated PR; only a NEWLY touched test file must pass. Can also be run
directly against any path for local backfill work (P6.1).
Depends on: nothing but the stdlib (`ast`, `argparse`, `pathlib`).

Allowlist (deliberately shallow test): add `# assertion-theater: allow` as
a trailing comment on the `def test_...(` line, OR anywhere in the test's
docstring.

Usage:
    python scripts/lint_assertion_theater.py [FILE_OR_DIR ...]
    # defaults to `tests/` when no paths are given
Exit code: 1 if any (non-allowlisted) violation is found, 0 otherwise.

Limitation (documented, not a bug): assertions are counted via `ast.walk()`
over the whole function body, so a test with a nested helper `def` sharing
its own bare asserts would be double-counted. No such pattern exists in this
codebase's test suite today; if one appears, exclude it via the allowlist
comment above rather than special-casing the walk.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ALLOW_MARKER = "assertion-theater: allow"

TestFunc = ast.FunctionDef | ast.AsyncFunctionDef


def _is_status_200_compare(compare: ast.Compare) -> bool:
    """True for `X.status_code == 200` in either operand order."""
    if len(compare.ops) != 1 or not isinstance(compare.ops[0], ast.Eq):
        return False
    operands = [compare.left, *compare.comparators]
    has_status_code_attr = any(isinstance(o, ast.Attribute) and o.attr == "status_code" for o in operands)
    has_200_literal = any(isinstance(o, ast.Constant) and o.value == 200 for o in operands)
    return has_status_code_attr and has_200_literal


def _is_not_none_compare(compare: ast.Compare) -> bool:
    """True for `X is not None`."""
    if len(compare.ops) != 1 or not isinstance(compare.ops[0], ast.IsNot):
        return False
    return any(isinstance(o, ast.Constant) and o.value is None for o in compare.comparators)


def _is_theater_assert(node: ast.Assert) -> bool:
    test = node.test
    if not isinstance(test, ast.Compare):
        return False
    return _is_status_200_compare(test) or _is_not_none_compare(test)


def _is_allowlisted(func: TestFunc, source_lines: list[str]) -> bool:
    if 0 < func.lineno <= len(source_lines) and ALLOW_MARKER in source_lines[func.lineno - 1]:
        return True
    docstring = ast.get_docstring(func) or ""
    return ALLOW_MARKER in docstring


def _find_test_functions(tree: ast.Module) -> list[TestFunc]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test_")
    ]


def _collect_asserts(func: TestFunc) -> list[ast.Assert]:
    return [node for node in ast.walk(func) if isinstance(node, ast.Assert)]


def check_file(path: Path) -> list[tuple[int, str]]:
    """Return [(lineno, function_name), ...] violations for one file."""
    try:
        source = path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        print(f"::warning::skipping {path} -- could not read ({exc})", file=sys.stderr)
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        print(f"::warning::skipping {path} -- syntax error ({exc})", file=sys.stderr)
        return []

    source_lines = source.splitlines()
    violations = []
    for func in _find_test_functions(tree):
        asserts = _collect_asserts(func)
        if len(asserts) != 1:
            continue  # only a SOLE assertion counts as "theater"
        if not _is_theater_assert(asserts[0]):
            continue
        if _is_allowlisted(func, source_lines):
            continue
        violations.append((func.lineno, func.name))
    return violations


def _resolve_paths(raw_paths: list[str]) -> list[Path]:
    files: set[Path] = set()
    for raw in raw_paths:
        p = Path(raw)
        if p.is_dir():
            files.update(p.rglob("test_*.py"))
        elif p.is_file():
            files.add(p)
        else:
            print(f"::warning::{raw} does not exist -- skipping", file=sys.stderr)
    return sorted(files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", default=["tests"], help="test files/dirs to check (default: tests/)")
    args = parser.parse_args(argv)

    files = _resolve_paths(args.paths)
    total_violations = 0
    for f in files:
        for lineno, name in check_file(f):
            print(f"{f}:{lineno}: {name}() -- only assertion is a bare status_code==200 / is-not-None check")
            total_violations += 1

    if total_violations:
        print(
            f"\n{total_violations} assertion-theater violation(s) in {len(files)} file(s). "
            f"Seed matching + non-matching rows and assert on rendered content instead, "
            f"or add `# {ALLOW_MARKER}` if the shallow check is deliberate.",
            file=sys.stderr,
        )
        return 1
    print(f"No assertion-theater violations in {len(files)} file(s) checked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
