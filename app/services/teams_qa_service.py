"""Teams Q&A service — delivers knowledge questions via Adaptive Cards in Teams DMs.

Builds batched question cards, handles card-submit answers, enforces daily
question caps, and sends digest summaries. Uses Graph API to deliver
Adaptive Cards as 1:1 chat messages.

Called by: app/routers/knowledge.py, app/jobs/knowledge_jobs.py
Depends on: app/models/knowledge.py, app/services/knowledge_service.py,
            app/utils/graph_client.py
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeConfig, KnowledgeEntry

# ═══════════════════════════════════════════════════════════════════════
#  QUESTION CAP
# ═══════════════════════════════════════════════════════════════════════


def get_question_cap(db: Session) -> int:
    """Read daily_question_cap from KnowledgeConfig, default 10."""
    row = db.query(KnowledgeConfig).filter(KnowledgeConfig.key == "daily_question_cap").first()
    if row:
        try:
            return int(row.value)
        except (ValueError, TypeError):
            pass
    return 10


def get_questions_asked_today(db: Session, user_id: int) -> int:
    """Count questions created today by this user."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        db.query(func.count(KnowledgeEntry.id))
        .filter(
            KnowledgeEntry.entry_type == "question",
            KnowledgeEntry.created_by == user_id,
            KnowledgeEntry.created_at >= today_start,
        )
        .scalar()
        or 0
    )


def check_question_quota(db: Session, user_id: int) -> dict:
    """Return quota status: {used, limit, remaining, allowed}."""
    limit = get_question_cap(db)
    used = get_questions_asked_today(db, user_id)
    remaining = max(0, limit - used)
    return {
        "used": used,
        "limit": limit,
        "remaining": remaining,
        "allowed": remaining > 0,
    }


# ═══════════════════════════════════════════════════════════════════════
#  ADAPTIVE CARD BUILDERS
# ═══════════════════════════════════════════════════════════════════════


def _build_question_card_item(question: KnowledgeEntry, is_nudge: bool = False) -> list:
    """Build body elements for one question inside a batch card.

    Returns a list of AdaptiveCard body elements: optional urgency banner,
    context line, question text, input field, and separator.
    """
    elements = []

    # Urgency banner for nudged questions
    if is_nudge:
        elements.append(
            {
                "type": "TextBlock",
                "text": "NEEDS ATTENTION — asked over 4 hours ago",
                "color": "Attention",
                "weight": "Bolder",
                "size": "Small",
            }
        )

    # Context line: req#, MPN, asker
    context_parts = []
    if question.requisition_id:
        context_parts.append(f"Req #{question.requisition_id}")
    if question.mpn:
        context_parts.append(f"MPN: {question.mpn}")
    if question.creator:
        name = getattr(question.creator, "name", None) or getattr(question.creator, "email", "Unknown")
        context_parts.append(f"Asked by: {name}")
    context_text = " | ".join(context_parts) if context_parts else "General question"

    elements.append(
        {
            "type": "TextBlock",
            "text": context_text,
            "size": "Small",
            "isSubtle": True,
            "wrap": True,
        }
    )

    # Question text
    elements.append(
        {
            "type": "TextBlock",
            "text": question.content,
            "wrap": True,
            "weight": "Bolder",
        }
    )

    # Input field for the answer
    elements.append(
        {
            "type": "Input.Text",
            "id": f"answer_{question.id}",
            "placeholder": "Type your answer here...",
            "isMultiline": True,
        }
    )

    # Separator
    elements.append(
        {
            "type": "TextBlock",
            "text": "---",
            "separator": True,
            "spacing": "Medium",
        }
    )

    return elements


def build_question_batch_card(questions: list) -> dict:
    """Wrap multiple question items into a single Adaptive Card.

    Title: "You have N questions to review"
    Submit action sends action=submit_answers with question_ids list.
    """
    body = [
        {
            "type": "TextBlock",
            "text": f"You have {len(questions)} question{'s' if len(questions) != 1 else ''} to review",
            "size": "Large",
            "weight": "Bolder",
        }
    ]

    question_ids = []
    now = datetime.now(timezone.utc)
    for q in questions:
        is_nudge = q.created_at and (now - q.created_at) > timedelta(hours=4)
        body.extend(_build_question_card_item(q, is_nudge=is_nudge))
        question_ids.append(q.id)

    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
        "actions": [
            {
                "type": "Action.Submit",
                "title": "Submit Answers",
                "data": {
                    "action": "submit_answers",
                    "question_ids": question_ids,
                },
            }
        ],
    }
    return card


def build_digest_card(answered_questions: list, pending_count: int) -> dict | None:
    """Build a daily digest card.

    Shows pending question count (Attention color) and recently answered
    questions (Q: .../A: ...). Returns None if nothing to show.
    """
    if not answered_questions and pending_count == 0:
        return None

    body = [
        {
            "type": "TextBlock",
            "text": "Knowledge Digest",
            "size": "Large",
            "weight": "Bolder",
        }
    ]

    if pending_count > 0:
        body.append(
            {
                "type": "TextBlock",
                "text": f"{pending_count} question{'s' if pending_count != 1 else ''} still pending your answer",
                "color": "Attention",
                "weight": "Bolder",
                "wrap": True,
            }
        )

    if answered_questions:
        body.append(
            {
                "type": "TextBlock",
                "text": "Recently answered:",
                "weight": "Bolder",
                "spacing": "Medium",
            }
        )
        for q in answered_questions:
            answer_text = ""
            if q.answers:
                answer_text = q.answers[0].content[:200]
            body.append(
                {
                    "type": "TextBlock",
                    "text": f"**Q:** {q.content[:150]}",
                    "wrap": True,
                }
            )
            body.append(
                {
                    "type": "TextBlock",
                    "text": f"**A:** {answer_text}" if answer_text else "**A:** (no answer text)",
                    "wrap": True,
                    "isSubtle": True,
                }
            )
            body.append(
                {
                    "type": "TextBlock",
                    "text": "---",
                    "separator": True,
                }
            )

    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
    }


# ═══════════════════════════════════════════════════════════════════════
#  BATCH DELIVERY
# ═══════════════════════════════════════════════════════════════════════


async def deliver_question_batch(db: Session, user_id: int) -> int:
    """Deliver undelivered, unresolved questions assigned to a user.

    Sends a batch Adaptive Card via DM, marks delivered_at (and nudged_at
    for questions older than 4 hours). Returns the count delivered.
    """
    from app.models.auth import User

    user = db.get(User, user_id)
    if not user:
        logger.warning("deliver_question_batch: user %d not found", user_id)
        return 0

    # Query undelivered, unresolved questions assigned to this user
    # Uses PostgreSQL JSON containment operator for assigned_to_ids
    questions = (
        db.query(KnowledgeEntry)
        .filter(
            KnowledgeEntry.entry_type == "question",
            KnowledgeEntry.is_resolved.is_(False),
            KnowledgeEntry.delivered_at.is_(None),
            sa_text("assigned_to_ids::jsonb @> :uid_json").bindparams(uid_json=f"[{user_id}]"),
        )
        .order_by(KnowledgeEntry.created_at.asc())
        .all()
    )

    if not questions:
        return 0

    now = datetime.now(timezone.utc)
    card = build_question_batch_card(questions)
    await _send_adaptive_card_dm(user, card, db)

    # Mark delivered and nudged
    for q in questions:
        q.delivered_at = now
        if q.created_at and (now - q.created_at) > timedelta(hours=4):
            q.nudged_at = now
    db.commit()

    logger.info("Delivered %d questions to user %d (%s)", len(questions), user_id, user.email)
    return len(questions)


async def deliver_knowledge_digest(db: Session, user_id: int) -> bool:
    """Send a daily digest card to a user.

    Includes: count of pending questions assigned to user, and questions
    asked by user that were answered in the last 24 hours. Skips if empty.
    Returns True if a digest was sent.
    """
    from app.models.auth import User

    user = db.get(User, user_id)
    if not user:
        logger.warning("deliver_knowledge_digest: user %d not found", user_id)
        return False

    # Count pending questions assigned to this user
    pending_count = (
        db.query(func.count(KnowledgeEntry.id))
        .filter(
            KnowledgeEntry.entry_type == "question",
            KnowledgeEntry.is_resolved.is_(False),
            sa_text("assigned_to_ids::jsonb @> :uid_json").bindparams(uid_json=f"[{user_id}]"),
        )
        .scalar()
        or 0
    )

    # Questions asked by this user that got answered in last 24 hours
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    answered_questions = (
        db.query(KnowledgeEntry)
        .filter(
            KnowledgeEntry.entry_type == "question",
            KnowledgeEntry.created_by == user_id,
            KnowledgeEntry.is_resolved.is_(True),
            KnowledgeEntry.updated_at >= cutoff,
        )
        .order_by(KnowledgeEntry.updated_at.desc())
        .all()
    )

    card = build_digest_card(answered_questions, pending_count)
    if card is None:
        return False

    await _send_adaptive_card_dm(user, card, db)
    logger.info("Delivered knowledge digest to user %d (%s)", user_id, user.email)
    return True


# ═══════════════════════════════════════════════════════════════════════
#  CARD ACTION HANDLER
# ═══════════════════════════════════════════════════════════════════════


async def handle_card_answer(db: Session, payload: dict) -> dict:
    """Handle a submit_answers card action.

    Extracts question_ids and answer_<id> fields from the card data,
    resolves the submitting user, and calls knowledge_service.post_answer()
    for each non-empty answer.

    Returns: {answered: int, total: int}
    """
    from app.models.auth import User
    from app.services import knowledge_service

    data = payload.get("data", {})
    question_ids = data.get("question_ids", [])
    user_aad_id = payload.get("from", {}).get("aadObjectId", "")

    user = db.query(User).filter(User.azure_id == user_aad_id).first() if user_aad_id else None
    if not user:
        logger.warning("handle_card_answer: could not resolve user %s", user_aad_id)
        return {"answered": 0, "total": len(question_ids)}

    answered = 0
    for qid in question_ids:
        answer_text = data.get(f"answer_{qid}", "").strip()
        if not answer_text:
            continue

        try:
            entry = knowledge_service.post_answer(
                db,
                user_id=user.id,
                question_id=qid,
                content=answer_text,
                answered_via="teams",
            )
            if entry:
                answered += 1
        except Exception as e:
            logger.error("handle_card_answer: failed to post answer for question %d: %s", qid, e)

    logger.info("handle_card_answer: answered %d/%d questions (user %s)", answered, len(question_ids), user.email)
    return {"answered": answered, "total": len(question_ids)}


# ═══════════════════════════════════════════════════════════════════════
#  GRAPH API ADAPTIVE CARD DM
# ═══════════════════════════════════════════════════════════════════════


async def _send_adaptive_card_dm(user, card: dict, db: Session) -> None:
    """Send an Adaptive Card as a 1:1 Teams DM via Graph API.

    Creates (or gets) a 1:1 chat with the user and posts a message with
    the card as an attachment. Silently skips if no valid token is available.

    Args:
        user: User model instance (needs .email, .access_token)
        card: Adaptive Card dict (full card body with $schema, type, version)
        db: DB session for token refresh
    """
    if not user.access_token and not db:
        logger.debug("No token for %s, skipping Adaptive Card DM", user.email)
        return
    try:
        from app.utils.graph_client import GraphClient

        if db:
            from app.scheduler import get_valid_token

            token = await get_valid_token(user, db)
        else:
            token = user.access_token
        if not token:
            logger.debug("No valid token for %s, skipping Adaptive Card DM", user.email)
            return

        gc = GraphClient(token)

        # Create or get 1:1 chat with the user
        chat = await gc.post_json(
            "/chats",
            {
                "chatType": "oneOnOne",
                "members": [
                    {
                        "@odata.type": "#microsoft.graph.aadUserConversationMember",
                        "roles": ["owner"],
                        "user@odata.bind": f"https://graph.microsoft.com/v1.0/users/{user.email}",
                    }
                ],
            },
        )
        chat_id = chat.get("id")
        if not chat_id:
            logger.warning("Failed to create/get chat for %s", user.email)
            return

        # Post message with Adaptive Card attachment
        await gc.post_json(
            f"/chats/{chat_id}/messages",
            {
                "body": {"contentType": "html", "content": ""},
                "attachments": [
                    {
                        "id": "adaptive-card",
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": card,
                    }
                ],
            },
        )
        logger.info("Adaptive Card DM sent to %s", user.email)
    except Exception as e:
        logger.debug(
            "Adaptive Card DM to %s failed (may not have Chat permissions): %s",
            user.email,
            e,
        )
