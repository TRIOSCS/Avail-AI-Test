"""manufacturer_alias_harvester.py — queue AI-proposed manufacturer aliases (idea #11).

manufacturer strings that miss normalize_brand_name (return verbatim) accumulate
silently as variants ("Seagate"/"SEAGATE"/"Seagate Technology"). This nightly harvester
collects the distinct unmatched strings across offers/sightings/requirements, asks
Claude to map each to an existing canonical or 'new'/'unknown', and queues the proposals
for human approval (the spec_codes pending pattern). Approving appends the variant to
the canonical Manufacturer.aliases JSON — it NEVER rewrites raw source-reported
manufacturer columns (verifier build note).

Called by: jobs (nightly harvest), routers/admin/manufacturer_aliases.py (approve/reject).
Depends on: manufacturer_normalizer (canonical map + garbage filter), utils/claude_client.
"""

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.constants import ManufacturerAliasStatus
from app.models.auth import User
from app.models.offers import Offer
from app.models.sourcing import Manufacturer, ManufacturerAliasPending, Requirement, Sighting
from app.services.manufacturer_normalizer import _load_map, invalidate_canonical_map, is_garbage_brand_value
from app.utils.claude_client import claude_structured
from app.utils.claude_errors import ClaudeError

_HARVEST_LIMIT = 200

_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["existing", "new", "unknown"]},
        "canonical": {
            "type": ["string", "null"],
            "description": "The existing canonical maker name this variant maps to (kind=existing), else null",
        },
        "reason": {"type": ["string", "null"]},
    },
    "required": ["kind"],
}


def _system(canonicals: list[str]) -> str:
    listed = ", ".join(sorted(canonicals)[:400])
    return (
        "You classify a raw electronic-component MANUFACTURER string against a list of "
        "known canonical maker names.\n"
        "- kind='existing' + canonical=<exact name from the list> when the string is a "
        "spelling/case/abbreviation variant of a listed maker (e.g. 'SEAGATE' -> "
        "'Seagate Technology').\n"
        "- kind='new' when it is clearly a real manufacturer NOT in the list.\n"
        "- kind='unknown' when it is a fragment, distributor, or you are unsure. NEVER guess.\n\n"
        f"Known canonical names: {listed}"
    )


def _distinct_manufacturer_strings(db: Session) -> set[str]:
    """Every distinct non-empty manufacturer string across the source tables."""
    out: set[str] = set()
    for col in (Offer.manufacturer, Sighting.manufacturer, Requirement.manufacturer, Requirement.brand):
        for (val,) in db.execute(select(col).where(col.isnot(None), col != "").distinct()).all():
            if val:
                out.add(str(val).strip())
    return out


async def harvest_manufacturer_aliases(db: Session, *, limit: int = _HARVEST_LIMIT) -> int:
    """Nightly: classify unmatched manufacturer strings and queue proposals. Returns
    the number of new pending rows. Commits. Best-effort per string."""
    canon_map = _load_map(db)  # {lower: canonical}
    canonicals = sorted(set(canon_map.values()))
    # Any variant ever queued — including REJECTED rows (kept as a marker) — so a
    # rejected string is never re-classified/re-billed and re-queued. Keyed on the
    # normalized form so case-only twins ("SEAGATE"/"seagate") count as one.
    already_pending = {v for (v,) in db.execute(select(ManufacturerAliasPending.variant_normalized)).all()}

    # Dedup candidates on their normalized form: one AI call + one pending row per
    # normalized value, not per case-variant. Sorted iteration keeps the representative
    # deterministic.
    candidates: dict[str, str] = {}
    for s in sorted(_distinct_manufacturer_strings(db)):
        if not s or is_garbage_brand_value(s):
            continue
        key = s.lower()
        if key in canon_map or key in already_pending:  # already a maker/alias, or already seen
            continue
        candidates.setdefault(key, s)

    unmatched = sorted(candidates.values())[:limit]
    if not unmatched:
        return 0

    system = _system(canonicals)
    created = 0
    for variant in unmatched:
        try:
            result = await claude_structured(
                variant,
                _SCHEMA,
                system=system,
                model_tier="fast",
                max_tokens=256,
                max_attempts=1,
                cost_bucket="mfr_alias_harvest",
            )
        except ClaudeError as e:
            logger.warning("manufacturer alias harvest failed for {!r}: {}", variant, e)
            continue
        if not isinstance(result, dict):
            continue
        kind = result.get("kind")
        if kind not in ("existing", "new", "unknown"):
            continue
        canonical = str(result["canonical"]).strip() if kind == "existing" and result.get("canonical") else None
        # A canonical the model invented but which isn't actually in the table → demote
        # to 'unknown' so a human decides rather than trusting a hallucinated mapping.
        if kind == "existing" and (not canonical or canonical.lower() not in canon_map):
            kind, canonical = "unknown", None
        db.add(
            ManufacturerAliasPending(
                variant=variant,
                variant_normalized=variant.lower(),
                proposed_canonical=canonical,
                proposed_kind=kind,
                source="ai",
                reason=(str(result.get("reason")).strip()[:500] if result.get("reason") else None),
            )
        )
        try:
            db.commit()
            created += 1
        except IntegrityError:
            db.rollback()  # raced onto the unique variant — fine
    return created


def approve_manufacturer_alias(db: Session, pending_id: int, user: User) -> Manufacturer:
    """Promote a pending proposal: append the variant to its canonical's aliases
    ('existing') or create the canonical ('new'), then dequeue and bust the alias-map
    cache. 'unknown' is refused (raises ValueError). Returns the canonical Manufacturer."""
    p = db.get(ManufacturerAliasPending, pending_id)
    if p is None:
        raise KeyError("pending alias not found")
    # 'unknown' is the classifier's "fragment / distributor / not sure" bucket — approving
    # it would mint a canonical maker from a raw string the AI flagged as probably-not-a-maker.
    # It is reject-only; requeue as 'new'/'existing' if it really is a maker.
    if p.proposed_kind == "unknown":
        raise ValueError("cannot approve an 'unknown' proposal into a canonical maker; reject it instead")

    variant = p.variant  # capture before delete — p is expired after commit
    target = p.proposed_canonical or p.variant
    m = db.query(Manufacturer).filter(Manufacturer.canonical_name == target).first()
    if m is None:
        m = Manufacturer(canonical_name=target, aliases=[])
        db.add(m)
        db.flush()
    if p.proposed_kind == "existing" and p.proposed_canonical:
        # Append the variant as an alias (JSON reassignment so SQLAlchemy tracks it).
        aliases = list(m.aliases or [])
        if variant not in aliases:
            aliases.append(variant)
        m.aliases = aliases
    db.delete(p)
    db.commit()
    # A new canonical/alias just landed — drop the memoized alias map so normalize_brand_name
    # sees it immediately (and the harvester stops re-proposing it) without a restart.
    invalidate_canonical_map()
    logger.info("manufacturer alias approved: {!r} → {!r} by {}", variant, target, getattr(user, "email", "?"))
    return m


def reject_manufacturer_alias(db: Session, pending_id: int) -> None:
    """Dismiss a proposal by flipping its status to 'rejected' — the row is KEPT.

    The raw variant still sits in the source manufacturer columns, so deleting the row
    would let the nightly harvester re-collect it, pay Claude to re-classify it, and re-
    queue it forever. The retained 'rejected' row is the marker that keeps it out of the
    harvest candidate set (spec_codes blacklist pattern). Idempotent.
    """
    p = db.get(ManufacturerAliasPending, pending_id)
    if p is not None:
        p.status = ManufacturerAliasStatus.REJECTED.value
        db.commit()
