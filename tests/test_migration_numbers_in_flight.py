"""Guard: the migration-number coordination log stays unique and complete.

MIGRATION_NUMBERS_IN_FLIGHT.txt (repo root) is the discovery path for alembic
migration numbers claimed by open branches — without it, two branches pick the same
next number, chain onto the same parent, and `alembic upgrade head` fails at deploy
time with two heads (tests/test_migration_chain.py only catches that AFTER the
collision exists, on whichever branch lands second). This test enforces the log's two
invariants:

1. No number is claimed twice (a duplicate claim IS the collision — fail before merge).
2. Every numbered migration file from the registry's start onward has a claim line
   (the log rots the first time a branch lands a migration without appending; making
   that a CI failure keeps the protocol self-sustaining).

Called by: pytest. Depends on: MIGRATION_NUMBERS_IN_FLIGHT.txt + alembic/versions/
filenames only (no DB).
"""

from __future__ import annotations

import pathlib
import re
from collections import Counter

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_LOG = _REPO_ROOT / "MIGRATION_NUMBERS_IN_FLIGHT.txt"
_VERSIONS = _REPO_ROOT / "alembic" / "versions"

# Claim line: "<3-digit number> <branch> <notes...>" ('#' lines are comments).
_CLAIM = re.compile(r"^(\d{3})\s+(\S+)")

# The registry starts at 097 — the first number claimed after the log was introduced.
# Earlier numbered migrations (090-096) pre-date it and are deliberately unlisted.
_REGISTRY_START = 97


def _claims() -> list[tuple[int, str]]:
    claims = []
    for line in _LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _CLAIM.match(line)
        assert m, f"Unparseable claim line in {_LOG.name}: {line!r} — expected '<3-digit number> <branch> <notes...>'"
        claims.append((int(m.group(1)), m.group(2)))
    return claims


def test_no_duplicate_number_claims():
    counts = Counter(num for num, _branch in _claims())
    dupes = sorted(num for num, n in counts.items() if n > 1)
    assert not dupes, (
        f"Migration number(s) {dupes} claimed more than once in {_LOG.name} — two "
        "branches picked the same number; the later one must renumber BEFORE merging "
        "or alembic ends up with two heads at deploy time."
    )


def test_every_landed_numbered_migration_is_claimed():
    claimed = {num for num, _branch in _claims()}
    landed = {
        int(m.group(1))
        for f in _VERSIONS.glob("*.py")
        if (m := re.match(r"(\d{3})_", f.name)) and int(m.group(1)) >= _REGISTRY_START
    }
    missing = sorted(landed - claimed)
    assert not missing, (
        f"Numbered migration(s) {missing} exist in alembic/versions/ without a claim "
        f"line in {_LOG.name} — append '<number> <branch> <notes>' in the same PR that "
        "adds a migration (see the protocol in the file header)."
    )
