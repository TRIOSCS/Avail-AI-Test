"""vendor_duplicates.py — vendor duplicate-check service (exact + fuzzy).

The single importable home for the duplicate logic that previously lived inline
in the /api/vendors/check-duplicate route: an EXACT normalized-name match
short-circuits as the one confident duplicate (score 100); otherwise fuzzy
candidates (suggestions, threshold 80) come from PostgreSQL pg_trgm
similarity() (index-backed) with a Python-side rapidfuzz fallback for SQLite /
environments without pg_trgm.

Called by: app/routers/vendors_crud.py (GET /api/vendors/check-duplicate) and
           app/routers/sightings.py (POST /v2/partials/sightings/composer-vendor)
           — both call this function directly, never loopback HTTP.
Depends on: models.VendorCard, vendor_utils.normalize_vendor_name, rapidfuzz
            (fallback path only).
"""

from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from ..models import VendorCard
from ..utils.vendor_helpers import find_vendor_card_by_name
from ..vendor_utils import normalize_vendor_name

FUZZY_MATCH_POOL_SIZE = 500  # Max vendors loaded for fuzzy duplicate check
TRIGRAM_SIMILARITY_THRESHOLD = 0.3  # pg_trgm similarity threshold (0.3 ≈ 80+ rapidfuzz score)


def _fuzzy_match_pg_trgm(db: Session, norm: str) -> list[dict]:
    """Use PostgreSQL pg_trgm similarity() for index-backed fuzzy matching."""
    sim = sqlfunc.similarity(VendorCard.normalized_name, norm).label("score")
    rows = (
        db.query(VendorCard.id, VendorCard.display_name, sim)
        .filter(sim >= TRIGRAM_SIMILARITY_THRESHOLD)
        .order_by(sim.desc())
        .limit(5)
        .all()
    )
    return [
        {
            "id": row.id,
            "name": row.display_name,
            "match": "fuzzy",
            "score": round(row.score * 100),
        }
        for row in rows
    ]


def _fuzzy_match_python(db: Session, norm: str) -> list[dict]:
    """Fallback O(n) fuzzy match using rapidfuzz (for SQLite / environments without
    pg_trgm)."""
    try:
        from rapidfuzz import fuzz
    except ImportError:  # pragma: no cover
        return []

    existing = (
        db.query(VendorCard.id, VendorCard.normalized_name, VendorCard.display_name).limit(FUZZY_MATCH_POOL_SIZE).all()
    )
    matches = []
    for row in existing:
        score = fuzz.token_sort_ratio(norm, row.normalized_name)
        if score >= 80:
            matches.append(
                {
                    "id": row.id,
                    "name": row.display_name,
                    "match": "fuzzy",
                    "score": round(score),
                }
            )
    matches.sort(key=lambda m: m["score"], reverse=True)
    return matches[:5]


def check_vendor_duplicate(name: str, db: Session) -> list[dict]:
    """Duplicate-check a vendor name: exact + fuzzy matches, capped at 5.

    An exact normalized-name match returns immediately as the single confident
    duplicate ({"match": "exact", "score": 100}); otherwise fuzzy candidates
    ({"match": "fuzzy", "score": >= 80}) are merely suggestions for the caller
    to surface. Uses the pg_trgm trigram index on PostgreSQL, falling back to
    Python-side rapidfuzz on SQLite or when pg_trgm is unavailable.
    """
    norm = normalize_vendor_name(name)

    # Exact match — the one confident duplicate, short-circuits fuzzy entirely
    exact = find_vendor_card_by_name(name, db)
    if exact:
        return [
            {
                "id": exact.id,
                "name": exact.display_name,
                "match": "exact",
                "score": 100,
            }
        ]

    # Fuzzy matches — use pg_trgm on PostgreSQL, rapidfuzz fallback on SQLite
    dialect = db.bind.dialect.name if db.bind else ""
    if dialect == "postgresql":
        try:
            matches = _fuzzy_match_pg_trgm(db, norm)
        except (OperationalError, ProgrammingError):
            db.rollback()
            logger.warning("pg_trgm not available, falling back to Python fuzzy match")
            matches = _fuzzy_match_python(db, norm)
    else:
        matches = _fuzzy_match_python(db, norm)

    return matches[:5]
