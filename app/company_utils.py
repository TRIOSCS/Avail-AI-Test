"""Company name normalization and fuzzy dedup helpers."""

from .vendor_utils import normalize_vendor_name as normalize_company_name


def find_company_dedup_candidates(db, threshold: int = 85, limit: int = 50) -> list[dict]:
    """Find potential duplicate companies using fuzzy name matching.

    Queries active companies, normalizes names, and pairwise compares with
    token_sort_ratio.  Returns scored pairs with an auto_keep_id heuristic:
      1. More sites wins
      2. Tie → has account_owner_id wins
      3. Tie → is_strategic wins
      4. Tie → lower id (older record) wins
    """
    from rapidfuzz import fuzz
    from sqlalchemy import func

    from .models import Company, CustomerSite

    # Load up to 500 active companies with site counts
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

    # Normalize names up front
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

    seen_pairs: set[tuple] = set()
    candidates = []

    for i, a in enumerate(enriched):
        for b in enriched[i + 1 :]:
            pair_key = (min(a["id"], b["id"]), max(a["id"], b["id"]))
            if pair_key in seen_pairs:  # pragma: no cover
                continue

            score = fuzz.token_sort_ratio(a["norm"], b["norm"])
            if score >= threshold:
                seen_pairs.add(pair_key)

                # Auto-keep heuristic
                def _rank(x):
                    return (x["site_count"], int(x["has_owner"]), int(x["is_strategic"]), -x["id"])

                auto_keep = a if _rank(a) >= _rank(b) else b

                candidates.append(
                    {
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
                        "score": score,
                        "auto_keep_id": auto_keep["id"],
                    }
                )

            if len(candidates) >= limit:
                break
        if len(candidates) >= limit:
            break

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates
