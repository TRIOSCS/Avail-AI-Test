"""offer_lead_time.py — normalize Offer.lead_time free text into lead_time_days.

Purpose (survey idea #12, lead-time slice):
  Offer.lead_time is free text ("2-3wk", "stock", "4-6 weeks"), so offers can't be
  sorted/filtered by lead time. This fills the normalized integer companion
  lead_time_days:
    - apply_offer_lead_time(offer): at offer save, DETERMINISTIC only
      (utils.normalization.normalize_lead_time) — no AI on the interactive path.
    - backfill_offer_lead_times(db): a nightly sweep. Deterministic first for every
      unset row; then, for the free-text residue the deterministic parser can't
      resolve, ONE Haiku call hard-guarded to return null rather than guess, marking
      those rows lead_time_days_ai=True (→ the amber "AI — verify" chip). The AI
      value NEVER overrides a deterministic parse.

Called by: routers/htmx/offers/crud.py (save), jobs/offers_jobs.py (nightly).
Depends on: utils/normalization, utils/claude_client, utils/claude_errors.
"""

from datetime import UTC, datetime

from loguru import logger
from sqlalchemy.orm import Session

from app.models.offers import Offer
from app.utils.claude_client import claude_structured
from app.utils.claude_errors import ClaudeError
from app.utils.normalization import normalize_lead_time

_BACKFILL_LIMIT = 500

_AI_SCHEMA = {
    "type": "object",
    "properties": {
        "lead_time_days": {
            "type": ["integer", "null"],
            "description": "Lead time in whole days, or null if the text does not state a resolvable lead time",
        }
    },
    "required": ["lead_time_days"],
}

_AI_SYSTEM = """You convert a vendor's free-text lead time into a number of days.

Rules:
- Return an integer number of days ONLY when the text clearly implies one
  (e.g. "about 6 weeks" -> 42, "a month and a half" -> 45, "next Friday"-style
  relative phrasing you cannot resolve -> null).
- NEVER guess. If the text is a request to contact, a question, or otherwise does
  not state a resolvable lead time, return null.
- A range -> its midpoint, rounded to whole days."""


def apply_offer_lead_time(offer: Offer) -> None:
    """Set offer.lead_time_days from its free-text lead_time, deterministically.

    Save-path only — never calls AI. Leaves lead_time_days None (and _ai False) when the
    deterministic parser can't resolve the text; the nightly backfill's AI tier handles
    that residue.
    """
    offer.lead_time_days = normalize_lead_time(offer.lead_time)
    offer.lead_time_days_ai = False


async def backfill_offer_lead_times(db: Session, *, use_ai: bool = True, limit: int = _BACKFILL_LIMIT) -> int:
    """Nightly: fill lead_time_days for offers that have free-text lead_time but no
    normalized value. Deterministic first; AI (hard-guarded null) for the residue
    when use_ai. Returns the number of rows updated. Commits.
    """
    rows = (
        db.query(Offer)
        .filter(
            Offer.lead_time_days.is_(None),
            Offer.lead_time.isnot(None),
            Offer.lead_time != "",
        )
        .order_by(Offer.id.desc())
        .limit(limit)
        .all()
    )
    updated = 0
    residue: list[Offer] = []
    for o in rows:
        days = normalize_lead_time(o.lead_time)
        if days is not None:
            o.lead_time_days = days
            o.lead_time_days_ai = False
            updated += 1
        # The AI tier handles only text the deterministic parser can't resolve AND
        # that we haven't already asked Claude about (so a permanently-unparseable
        # lead time is never re-billed night after night).
        elif (o.lead_time or "").strip() and o.lead_time_ai_attempted_at is None:
            residue.append(o)
    if updated:
        db.commit()

    if not use_ai or not residue:
        return updated

    now = datetime.now(UTC)
    for o in residue:
        # Stamp BEFORE the call so a row is marked tried even if the call raises —
        # never re-bill it. (A future explicit re-try path can clear this stamp.)
        o.lead_time_ai_attempted_at = now
        try:
            result = await claude_structured(
                o.lead_time,
                _AI_SCHEMA,
                system=_AI_SYSTEM,
                model_tier="fast",
                max_tokens=128,
                max_attempts=1,
                cost_bucket="offer_lead_time",
            )
        except ClaudeError as e:
            logger.warning("offer lead-time AI backfill failed for offer {}: {}", o.id, e)
            continue
        days = result.get("lead_time_days") if isinstance(result, dict) else None
        # Deterministic still wins: only accept the AI value while days stays unset.
        if isinstance(days, int) and not isinstance(days, bool) and days >= 0 and o.lead_time_days is None:
            o.lead_time_days = days
            o.lead_time_days_ai = True
            updated += 1
    db.commit()
    return updated
