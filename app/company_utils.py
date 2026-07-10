"""Company name normalization and fuzzy dedup helpers."""

import re

from .vendor_utils import _SUFFIX_PATTERN
from .vendor_utils import normalize_vendor_name as normalize_company_name


def suggest_clean_company_name(name: str) -> str:
    """Return a display-cased name with legal suffixes / leading "the" stripped.

    The suggest-only counterpart to normalize_company_name (which lowercases for
    matching): this preserves the original casing so the chip can offer a human-friendly
    "Suggested name". Reuses the SAME suffix regex as the matcher, applied to the
    original-case string. Returns "" if the input is empty or normalizes to nothing.
    """
    if not name:
        return ""
    n = name.strip()
    for _ in range(3):
        prev = n
        n = re.sub(r",\s*$", "", n)
        n = _SUFFIX_PATTERN.sub("", n).strip()
        n = re.sub(r"[,.\-]+$", "", n).strip()
        if n == prev:
            break
    n = re.sub(r"^the\s+", "", n, flags=re.IGNORECASE)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _auto_keep_rank(company: dict) -> tuple[int, int, int, int]:
    """Sort key for the auto-keep heuristic (higher wins).

    Priority: more sites → has account owner → is strategic → lower id (older record).
    """
    return (
        company["site_count"],
        int(company["has_owner"]),
        int(company["is_strategic"]),
        -company["id"],
    )


def _pair_dict(a: dict, b: dict, score: float) -> dict:
    """Build one nested candidate dict (the public shape both backends emit).

    auto_keep_id follows the heuristic: more sites → has owner → is strategic → older id.
    """
    auto_keep = a if _auto_keep_rank(a) >= _auto_keep_rank(b) else b
    return {
        "company_a": {
            "id": a["id"],
            "name": a["name"],
            "site_count": a["site_count"],
            "has_owner": a["has_owner"],
        },
        "company_b": {
            "id": b["id"],
            "name": b["name"],
            "site_count": b["site_count"],
            "has_owner": b["has_owner"],
        },
        "score": int(score),
        "auto_keep_id": auto_keep["id"],
    }


def _find_company_dedup_candidates_pg(db, threshold: int, limit: int) -> list[dict]:
    """PostgreSQL path: pg_trgm self-join on normalized_name — no 500-row O(n^2) cap.

    Uses func.similarity() over the GIN(normalized_name gin_trgm_ops) index (migration
    120). The `%` operator (set via pg_trgm.similarity_threshold default 0.3) plus an
    explicit similarity() >= threshold/100 predicate keep only real near-dups. Pairs are
    deduplicated with a < b on id so each unordered pair appears once. site_count /
    has_owner / is_strategic are fetched per id afterward for the auto_keep heuristic and
    the rendered row.
    """
    from sqlalchemy import func, text

    from .models import Company, CustomerSite

    a = Company.__table__.alias("a")
    b = Company.__table__.alias("b")
    sim = func.similarity(a.c.normalized_name, b.c.normalized_name)

    pair_rows = (
        db.query(
            a.c.id.label("a_id"),
            a.c.name.label("a_name"),
            b.c.id.label("b_id"),
            b.c.name.label("b_name"),
            sim.label("sim"),
        )
        .filter(
            a.c.is_active.is_(True),
            b.c.is_active.is_(True),
            a.c.id < b.c.id,
            a.c.normalized_name.isnot(None),
            b.c.normalized_name.isnot(None),
            a.c.normalized_name != "",
            b.c.normalized_name != "",
            a.c.normalized_name.op("%")(b.c.normalized_name),
            sim >= (threshold / 100.0),
        )
        .order_by(text("sim DESC"))
        .limit(limit)
        .all()
    )

    if not pair_rows:
        return []

    # Fetch attributes for every company involved (one grouped query).
    ids = {r.a_id for r in pair_rows} | {r.b_id for r in pair_rows}
    attr_rows = (
        db.query(
            Company.id,
            Company.account_owner_id,
            Company.is_strategic,
            func.count(CustomerSite.id).label("site_count"),
        )
        .outerjoin(CustomerSite, CustomerSite.company_id == Company.id)
        .filter(Company.id.in_(ids))
        .group_by(Company.id)
        .all()
    )
    attrs = {
        r.id: {
            "site_count": r.site_count or 0,
            "has_owner": r.account_owner_id is not None,
            "is_strategic": bool(r.is_strategic),
        }
        for r in attr_rows
    }

    candidates = []
    for r in pair_rows:
        a_dict = {
            "id": r.a_id,
            "name": r.a_name,
            **attrs.get(r.a_id, {"site_count": 0, "has_owner": False, "is_strategic": False}),
        }
        b_dict = {
            "id": r.b_id,
            "name": r.b_name,
            **attrs.get(r.b_id, {"site_count": 0, "has_owner": False, "is_strategic": False}),
        }
        candidates.append(_pair_dict(a_dict, b_dict, round(r.sim * 100)))
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def _find_company_dedup_candidates_rapidfuzz(db, threshold: int, limit: int) -> list[dict]:
    """SQLite / fallback path: load active companies and pairwise token_sort_ratio.

    Preserves the original 500-row cap (rapidfuzz is O(n^2) in Python). Used by the test
    DB, which has no pg_trgm.
    """
    from sqlalchemy import func

    from .models import Company, CustomerSite
    from .vendor_utils import fuzzy_dedup_scan

    rows = (
        db.query(
            Company.id,
            Company.name,
            Company.account_owner_id,
            Company.is_strategic,
            func.count(CustomerSite.id).label("site_count"),
        )
        .outerjoin(CustomerSite, CustomerSite.company_id == Company.id)
        .filter(Company.is_active.is_(True))
        .group_by(Company.id)
        .order_by(Company.id)
        .limit(500)
        .all()
    )

    enriched = []
    for r in rows:
        norm = normalize_company_name(r.name)
        if norm:
            enriched.append(
                {
                    "id": r.id,
                    "name": r.name,
                    "norm": norm,
                    "site_count": r.site_count or 0,
                    "has_owner": r.account_owner_id is not None,
                    "is_strategic": bool(r.is_strategic),
                }
            )

    scanned = fuzzy_dedup_scan(enriched, lambda e: e["norm"], threshold=threshold, limit=limit)
    candidates = [_pair_dict(a, b, score) for a, b, score in scanned]

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def find_company_dedup_candidates(db, threshold: int = 85, limit: int = 50) -> list[dict]:
    """Find potential duplicate companies using fuzzy name matching.

    Returns scored pairs with an auto_keep_id heuristic (more sites → has account owner →
    is strategic → lower id). Each pair is nested:
        {"company_a": {id, name, site_count, has_owner},
         "company_b": {...}, "score": int, "auto_keep_id": int}

    Backend by dialect (same shape either way):
      - PostgreSQL: pg_trgm self-join on normalized_name via func.similarity() — drops the
        500-row O(n^2) cap (migration 120's GIN index).
      - SQLite / fallback: the original rapidfuzz token_sort_ratio scan (500-row cap),
        keeping the test DB green (pg_trgm is Postgres-only — feedback_sqlite_masks_postgres).
    """
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        return _find_company_dedup_candidates_pg(db, threshold, limit)
    return _find_company_dedup_candidates_rapidfuzz(db, threshold, limit)
