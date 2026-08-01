"""proactive_teams_push.py — Push new proactive matches into Teams.

Phase 1 of the Teams-agent / proactive plan (specs/plan-teams-agent-proactive.md).
The proactive engine already writes scored ProactiveMatch rows but they only
surface in-app. This service composes an Adaptive Card digest of the newly
created NEW matches and posts it to the shared Teams channel, so a salesperson
learns about them without opening AVAIL and clicking "refresh".

A SystemConfig high-water-mark on the match id (``proactive_teams_push_last_id``)
guarantees each match is pushed at most once — the job is safe to run on any
cadence and across restarts. Delivery reuses the existing outbound Adaptive Card
webhook path; nothing here talks to a bot.

Called by: app/jobs/offers_jobs.py (_job_proactive_teams_push)
Depends on: app.config, app.models, app.services.teams_notifications
"""

from loguru import logger
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import ProactiveMatchStatus
from ..models.config import SystemConfig
from ..models.crm import Company
from ..models.intelligence import ProactiveMatch
from .teams_notifications import post_teams_channel_card

_WATERMARK_KEY = "proactive_teams_push_last_id"

# Bound how many matches one run considers, and how many appear in the card body.
_SCAN_CAP = 500
_CARD_ROWS = 10


def _get_last_id(db: Session) -> int:
    """Highest ProactiveMatch id already pushed to Teams (0 if never)."""
    row = db.query(SystemConfig).filter(SystemConfig.key == _WATERMARK_KEY).first()
    if row and row.value and row.value.isdigit():
        return int(row.value)
    return 0


def _set_last_id(db: Session, match_id: int) -> None:
    """Persist the push high-water-mark to SystemConfig."""
    row = db.query(SystemConfig).filter(SystemConfig.key == _WATERMARK_KEY).first()
    if row:
        row.value = str(match_id)
    else:
        db.add(
            SystemConfig(
                key=_WATERMARK_KEY,
                value=str(match_id),
                description="Highest proactive match id pushed to Teams",
            )
        )
    db.commit()


def _build_card(matches: list[ProactiveMatch], names: dict[int, str], total: int) -> dict:
    """Build the Adaptive Card for a batch of new proactive matches."""
    header = f"🎯 {total} new proactive match{'es' if total != 1 else ''} in AVAIL"
    facts = []
    for m in matches[:_CARD_ROWS]:
        company = names.get(m.company_id, "Unknown customer") if m.company_id else "Unknown customer"
        facts.append({"title": m.mpn, "value": f"{company} — score {m.match_score or 0}"})
    body: list[dict] = [
        {"type": "TextBlock", "text": header, "weight": "Bolder", "size": "Medium", "wrap": True},
        {"type": "FactSet", "facts": facts},
    ]
    if total > _CARD_ROWS:
        body.append(
            {
                "type": "TextBlock",
                "text": f"+{total - _CARD_ROWS} more — open AVAIL to review.",
                "wrap": True,
                "isSubtle": True,
            }
        )
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
        "actions": [{"type": "Action.OpenUrl", "title": "Review in AVAIL", "url": f"{settings.app_url}/v2/proactive"}],
    }


async def push_new_matches_to_teams(db: Session) -> dict[str, int]:
    """Post a digest of not-yet-pushed NEW proactive matches to Teams.

    Idempotent via the ``proactive_teams_push_last_id`` watermark: only matches
    with an id above the mark are considered, and the mark advances to the highest
    id seen this run (whether or not it made the card body), so nothing repeats.

    Returns ``{"pushed": <count>}`` — 0 when there is nothing new.
    """
    last_id = _get_last_id(db)
    matches: list[ProactiveMatch] = (
        db.query(ProactiveMatch)
        .filter(ProactiveMatch.status == ProactiveMatchStatus.NEW, ProactiveMatch.id > last_id)
        .order_by(ProactiveMatch.match_score.desc(), ProactiveMatch.id.asc())
        .limit(_SCAN_CAP)
        .all()
    )
    if not matches:
        return {"pushed": 0}

    company_ids = {m.company_id for m in matches if m.company_id}
    names: dict[int, str] = {}
    if company_ids:
        for cid, cname in db.query(Company.id, Company.name).filter(Company.id.in_(company_ids)).all():
            names[cid] = cname

    total = len(matches)
    await post_teams_channel_card(_build_card(matches, names, total))

    max_id = max(m.id for m in matches)
    _set_last_id(db, max_id)
    logger.info(f"Proactive Teams push: sent digest of {total} new match(es), watermark now {max_id}")
    return {"pushed": total}
