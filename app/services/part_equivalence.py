"""Part-equivalence: AI-judged variant matching for proactive (2026-08-08).

Answers one question deterministically: which part-number spellings may pool
their supply and demand? Three tiers:

1. FORMATTING — spellings sharing one normalize_mpn_key ("LTSR15-NP" /
   "LTSR 15-NP") are the same part by construction; pooled automatically and
   labeled a formatting variant.
2. AI SUFFIX/NEAR-MISS — candidate key pairs (one a prefix of the other =
   ordering-suffix shape, or same length one character apart = near-miss/typo
   shape) are classified ONCE by Claude into same | different | uncertain and
   stored in part_equivalences. Only verdict='same' pools, and the UI
   color-codes those pooled lines as AI guesses to double-check.
3. HUMAN — a one-tap "not the same part" override writes source='human',
   which permanently outranks the AI.

Matching always consults the stored table, never the model — same inputs,
same matches, every scan. Absent or uncertain verdicts never pool.

Called by: services/proactive_matching (class expansion), app/jobs/offers_jobs
    (classification pass before the scan), routers/htmx/proactive (reject).
Depends on: models (PartEquivalence, Offer, Requirement), utils/claude_client,
    utils/normalization.
"""

import asyncio
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import Offer, PartEquivalence, Requirement, User
from ..utils.normalization import normalize_mpn_key

# Suffix candidates: the longer key extends the shorter by at most this many
# characters (ordering codes like E3, 08, TR, CT, RL are short).
MAX_SUFFIX_LEN = 6
# Safety valve per classification pass — pairs beyond this wait for the next run.
MAX_PAIRS_PER_PASS = 25

_CLASSIFIER_SYSTEM = """You are an electronic-components expert at a parts brokerage.
Given two manufacturer part numbers, decide whether they refer to the SAME orderable
component such that stock of one can be offered against demand for the other.

- "same": the difference is only a non-functional ordering code — packaging/carrier
  (tape & reel, tube, tray, cut tape), reel size, RoHS/lead-free marker (e.g. Vishay
  -E3, +, G suffixes), or pure formatting.
- "different": the difference changes what the part IS or its fitness — output
  voltage, capacity, speed/frequency, temperature grade, pin count, revision with
  functional impact — or the numbers belong to different part families (a one-character
  difference in the BASE, e.g. PSOT vs GSOT, is a different family or a typo, never "same").
- "uncertain": you cannot tell without a datasheet.

Be conservative: when in doubt, "uncertain", never "same".
Return ONLY valid JSON: {"verdict": "same"|"different"|"uncertain", "confidence": 0.0-1.0, "reason": "<one sentence>"}"""


def norm_key(raw: str | None) -> str:
    """Stable pooling key for a spelling (formatting-independent)."""
    return normalize_mpn_key(raw or "")


def _ordered(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _hamming_is_one(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    return sum(1 for x, y in zip(a, b, strict=True) if x != y) == 1


def find_candidate_pairs(spellings_by_key: dict[str, str]) -> list[tuple[str, str]]:
    """Key pairs worth asking the AI about, from {key: example spelling}.

    Suffix shape (prefix + ≤MAX_SUFFIX_LEN extra chars) or near-miss shape (same length,
    exactly one character apart). Formatting variants never get here — they share one
    key.
    """
    keys = sorted(spellings_by_key)
    pairs: list[tuple[str, str]] = []
    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            if b.startswith(a) and 0 < len(b) - len(a) <= MAX_SUFFIX_LEN:
                pairs.append((a, b))
            elif _hamming_is_one(a, b):
                pairs.append((a, b))
    return pairs


def observed_spellings(db: Session, keys: set[str]) -> dict[str, set[str]]:
    """Every raw spelling on file (offers ∪ requirements) for the given keys."""
    out: dict[str, set[str]] = {k: set() for k in keys}
    if not keys:
        return out
    for raw in db.scalars(select(Offer.mpn).where(Offer.normalized_mpn.in_(keys)).distinct()).all():
        out.setdefault(norm_key(raw), set()).add((raw or "").strip().upper())
    for raw in db.scalars(select(Requirement.primary_mpn).where(Requirement.normalized_mpn.in_(keys)).distinct()).all():
        out.setdefault(norm_key(raw), set()).add((raw or "").strip().upper())
    return out


def _load_verdicts(db: Session, keys: set[str]) -> list[PartEquivalence]:
    if not keys:
        return []
    return list(
        db.scalars(select(PartEquivalence).where(or_(PartEquivalence.key_a.in_(keys), PartEquivalence.key_b.in_(keys))))
    )


def expand_parts(db: Session, parts: set[str]) -> dict[str, dict]:
    """Equivalence classes for many display parts with BATCHED verdict loads.

    Per part: {"keys": [normalized keys to pool], "ai_variants": {key: reason}}
    — 'keys' always contains the part's own key; extra keys come only from
    stored verdict='same' pairs (human 'different' removes an AI 'same').
    Query count stays constant in the number of parts (perf guard).
    """
    out: dict[str, dict] = {}
    state: dict[str, dict] = {}
    frontier: set[str] = set()
    for p in parts:
        base = norm_key(p)
        if not base:
            out[p] = {"keys": [], "ai_variants": {}, "pool_source": {}}
            continue
        state[p] = {"keys": {base}, "ai_variants": {}}
        frontier.add(base)

    # Human 'different' verdicts are a HARD kill-switch (QC 2026-08-10 P4): a key
    # may never join a class that already contains a key a human ruled it different
    # from — NOT EVEN transitively. The bug: A≠B (human), but A~C and C~B (AI 'same')
    # pooled A and B anyway, silently overriding the human's "not the same part".
    blocked: set[tuple[str, str]] = set()

    # Verdicts form a tiny graph; walk 'same' edges (a variant of a variant
    # pools too), loading each round's edges for EVERY part in one query.
    for _ in range(3):  # bounded — ordering-code chains are short
        rows = _load_verdicts(db, frontier)
        if not rows:
            break
        best: dict[tuple[str, str], PartEquivalence] = {}
        for r in rows:
            if r.verdict == "different" and r.source == "human":
                blocked.add((r.key_a, r.key_b))  # stored key_a<key_b, matches _ordered()
            pair = (r.key_a, r.key_b)
            cur = best.get(pair)
            if cur is None or (r.source == "human" and cur.source != "human"):
                best[pair] = r
        frontier = set()
        for info in state.values():
            keys = info["keys"]
            for r in best.values():
                if r.verdict != "same":
                    continue
                if r.key_a in keys and r.key_b not in keys:
                    other = r.key_b
                elif r.key_b in keys and r.key_a not in keys:
                    other = r.key_a
                else:
                    continue
                # Kill-switch: refuse the pool if a human ruled `other` different from
                # ANY key already in this class (direct or transitive).
                if any(_ordered(other, k) in blocked for k in keys):
                    continue
                keys.add(other)
                info["ai_variants"][other] = r.reason or "AI: same part, ordering-code variant"
                # Source of the edge that ACTUALLY pooled this key into the class —
                # surfaces render the amber verify chip iff this is 'ai', regardless
                # of unrelated human edges elsewhere touching the same key.
                info.setdefault("pool_source", {})[other] = r.source
                frontier.add(other)
        if not frontier:
            break

    for p, info in state.items():
        out[p] = {
            "keys": sorted(info["keys"]),
            "ai_variants": info["ai_variants"],
            "pool_source": info.get("pool_source", {}),
        }
    return out


def expand_part(db: Session, part: str) -> dict:
    """Single-part convenience form of expand_parts."""
    return expand_parts(db, {part})[part]


def class_keys_and_spellings(db: Session, part: str) -> tuple[set[str], set[str]]:
    """Equivalence-class derivation shared by the proactive matcher passes.

    Returns ``(class_keys, class_spellings)``: the pooled normalized keys for demand
    joins, and every observed spelling (plus *part* itself) for suppression/dedup
    (throttle, do-not-offer, active-match) so a variant spelling can never sidestep a
    suppression. The ONE copy of the expand_part → observed_spellings union that the
    three matchers in proactive_matching previously each inlined.
    """
    class_keys = set(expand_part(db, part)["keys"])
    class_spellings = {part} | {s for group in observed_spellings(db, class_keys).values() for s in group}
    return class_keys, class_spellings


def windowed_spellings_by_key(db: Session) -> dict[str, str]:
    """{normalized key: example spelling} over the matching windows.

    Live in-window offers plus windowed requirements — the population whose candidate
    pairs are worth classifying.
    """
    from .proactive_matching import _LIVE_STATUSES, _offer_window_start, _requirement_window_start

    out: dict[str, str] = {}
    for raw in db.scalars(
        select(Offer.mpn).where(Offer.status.in_(_LIVE_STATUSES), Offer.created_at >= _offer_window_start()).distinct()
    ).all():
        key = norm_key(raw)
        if key:
            out.setdefault(key, (raw or "").strip().upper())
    for raw in db.scalars(
        select(Requirement.primary_mpn).where(Requirement.created_at >= _requirement_window_start()).distinct()
    ).all():
        key = norm_key(raw)
        if key:
            out.setdefault(key, (raw or "").strip().upper())
    return out


async def classify_new_pairs(
    db: Session,
    spellings_by_key: dict[str, str],
    *,
    limit: int = MAX_PAIRS_PER_PASS,
    involving: str | None = None,
) -> int:
    """Ask Claude about not-yet-classified candidate pairs; store verdicts.

    One call per pair, Haiku tier, verdict cached forever (a human can flip it).
    Failures skip the pair — it retries on a later pass. Commits.

    ``involving`` restricts classification to pairs touching that normalized key —
    the zero-hit search sweep uses it so its budget is spent on the part the user
    actually searched (and so rotating junk queries can only mint verdicts about
    their own key, never the whole window's backlog).
    """
    from ..utils.claude_client import claude_json

    # find_candidate_pairs is O(n^2) in the number of distinct keys; running it
    # inline would block the event loop when the window is large. Offload it to a
    # worker thread so the loop stays responsive — same pairs, same verdicts.
    pairs = await asyncio.get_running_loop().run_in_executor(None, find_candidate_pairs, spellings_by_key)
    if involving:
        pairs = [p for p in pairs if involving in p]
    if not pairs:
        return 0
    existing = {
        (r.key_a, r.key_b)
        for r in db.scalars(
            select(PartEquivalence).where(
                or_(
                    PartEquivalence.key_a.in_({a for a, _ in pairs}),
                    PartEquivalence.key_b.in_({b for _, b in pairs}),
                )
            )
        )
    }
    todo = [p for p in pairs if _ordered(*p) not in existing][:limit]
    stored = 0
    for key_a, key_b in todo:
        a, b = _ordered(key_a, key_b)
        raw_a, raw_b = spellings_by_key.get(a, a), spellings_by_key.get(b, b)
        try:
            result = await claude_json(
                f'Part number 1: "{raw_a}"\nPart number 2: "{raw_b}"\nSame orderable component?',
                system=_CLASSIFIER_SYSTEM,
                model_tier="fast",
                max_tokens=300,
                timeout=30,
                cost_bucket="part_equivalence",
            )
            verdict = str(result.get("verdict", "uncertain")).lower()
            if verdict not in ("same", "different", "uncertain"):
                verdict = "uncertain"
            db.add(
                PartEquivalence(
                    key_a=a,
                    key_b=b,
                    example_a=raw_a,
                    example_b=raw_b,
                    verdict=verdict,
                    confidence=float(result.get("confidence") or 0.0),
                    reason=str(result.get("reason") or "")[:2000],
                    source="ai",
                )
            )
            # Commit each verdict as it lands (QC 2026-08-08): the scan job caps
            # this pass with a hard timeout, and a single end-of-loop commit
            # discarded every already-paid Claude verdict when the timeout fired
            # mid-pass — then re-paid the identical calls next run. Per-verdict
            # commits make paid work durable.
            db.commit()
            stored += 1
        except Exception as exc:  # noqa: BLE001 — model down ≠ matching broken; pair retries next pass
            db.rollback()
            logger.warning("Part-equivalence classification failed for {} / {}: {}", raw_a, raw_b, exc)
    if stored:
        logger.info("Part-equivalence: classified {} new pair(s)", stored)
    return stored


def record_human_verdict(db: Session, user: User, raw_a: str, raw_b: str, verdict: str) -> PartEquivalence:
    """One-tap override from the UI ('not the same part' / 'confirmed same').

    Upserts the pair with source='human', which permanently outranks the AI. Caller
    commits.
    """
    if verdict not in ("same", "different"):
        raise ValueError(f"Unknown verdict: {verdict}")
    a, b = _ordered(norm_key(raw_a), norm_key(raw_b))
    if not a or not b or a == b:
        raise ValueError("Two distinct part numbers required")
    row = db.scalars(select(PartEquivalence).where(PartEquivalence.key_a == a, PartEquivalence.key_b == b)).first()
    if row:
        row.verdict = verdict
        row.source = "human"
        row.decided_by_id = user.id
        row.updated_at = datetime.now(UTC)
    else:
        row = PartEquivalence(
            key_a=a,
            key_b=b,
            example_a=raw_a.strip().upper(),
            example_b=raw_b.strip().upper(),
            verdict=verdict,
            confidence=1.0,
            reason=f"Human verdict by {user.email}",
            source="human",
            decided_by_id=user.id,
        )
        db.add(row)
    logger.info("Part-equivalence human verdict: {} / {} = {} by {}", raw_a, raw_b, verdict, user.email)
    return row
