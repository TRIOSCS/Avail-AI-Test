# Teams Q&A Routing + Daily Digest — Phase 2 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add Teams Adaptive Card Q&A answering, batched question delivery, daily question cap, 4h nudge, and daily knowledge digest.

**Architecture:** New `teams_qa_service.py` handles card building, batch delivery, digest, and card action processing. Migration 064 adds columns to `knowledge_entries` and `teams_alert_config`, plus a new `knowledge_config` table. Two new scheduler jobs deliver batches and digests hourly. Frontend adds quota display to the question modal.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, PostgreSQL, Alembic, APScheduler, Microsoft Graph API, Adaptive Cards

**No tests until all 5 AI Intelligence Layer phases are complete.**

---

### Task 1: Migration 064 — Schema changes

**Files:**
- Create: `alembic/versions/064_teams_qa_routing.py`
- Modify: `app/models/knowledge.py`
- Modify: `app/models/teams_alert_config.py`

**Step 1: Add columns to KnowledgeEntry model**

In `app/models/knowledge.py`, add after the `updated_at` column (line 50):

```python
# Phase 2: Teams Q&A routing
nudged_at = Column(DateTime(timezone=True), nullable=True)
delivered_at = Column(DateTime(timezone=True), nullable=True)
answered_via = Column(String(10), nullable=True)  # 'web' or 'teams'
```

**Step 2: Add column to TeamsAlertConfig model**

In `app/models/teams_alert_config.py`, add after `quiet_hours_end` (line 28):

```python
knowledge_digest_hour = Column(Integer, nullable=False, default=14, server_default="14")
```

**Step 3: Create the knowledge_config model**

Add to `app/models/knowledge.py` after the KnowledgeEntry class:

```python
class KnowledgeConfig(Base):
    __tablename__ = "knowledge_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(50), unique=True, nullable=False)
    value = Column(String(255), nullable=False)
```

**Step 4: Register KnowledgeConfig in models/__init__.py**

Add after the KnowledgeEntry import:

```python
from .knowledge import KnowledgeConfig  # noqa: F401
```

**Step 5: Write manual migration**

Create `alembic/versions/064_teams_qa_routing.py`:

```python
"""Teams Q&A routing — Phase 2 schema changes.

Revision ID: 064
Revises: 063
Create Date: 2026-03-07
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "064"
down_revision: Union[str, None] = "063"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add columns to knowledge_entries
    op.add_column("knowledge_entries", sa.Column("nudged_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("knowledge_entries", sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("knowledge_entries", sa.Column("answered_via", sa.String(10), nullable=True))

    # Add digest hour to teams_alert_config
    op.add_column("teams_alert_config", sa.Column("knowledge_digest_hour", sa.Integer(), nullable=False, server_default="14"))

    # Create knowledge_config table
    op.create_table(
        "knowledge_config",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(50), unique=True, nullable=False),
        sa.Column("value", sa.String(255), nullable=False),
    )

    # Seed default question cap
    op.execute("INSERT INTO knowledge_config (key, value) VALUES ('daily_question_cap', '10')")


def downgrade() -> None:
    op.drop_table("knowledge_config")
    op.drop_column("teams_alert_config", "knowledge_digest_hour")
    op.drop_column("knowledge_entries", "answered_via")
    op.drop_column("knowledge_entries", "delivered_at")
    op.drop_column("knowledge_entries", "nudged_at")
```

**Step 6: Commit**

```bash
git add app/models/knowledge.py app/models/teams_alert_config.py app/models/__init__.py alembic/versions/064_teams_qa_routing.py
git commit -m "feat(phase2): migration 064 — teams Q&A routing schema"
```

---

### Task 2: Teams Q&A Service — Card builders and batch delivery

**Files:**
- Create: `app/services/teams_qa_service.py`

**Step 1: Create the service file**

```python
"""Teams Q&A service — Adaptive Card building, batch delivery, digest, card action handling.

Builds Adaptive Cards for Q&A questions, delivers batched question cards to buyers
via Teams DM, handles card action submissions, and sends daily knowledge digests.

Called by: routers/teams_bot.py, routers/knowledge.py, jobs/knowledge_jobs.py
Depends on: services/knowledge_service.py, services/teams_alert_service.py, models
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeConfig, KnowledgeEntry
from app.models.teams_alert_config import TeamsAlertConfig


# ═══════════════════════════════════════════════════════════════════════
#  QUESTION CAP
# ═══════════════════════════════════════════════════════════════════════


def get_question_cap(db: Session) -> int:
    """Get the daily question cap from knowledge_config."""
    row = db.query(KnowledgeConfig).filter(KnowledgeConfig.key == "daily_question_cap").first()
    return int(row.value) if row else 10


def get_questions_asked_today(db: Session, user_id: int) -> int:
    """Count questions asked by this user today (UTC)."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        db.query(func.count(KnowledgeEntry.id))
        .filter(
            KnowledgeEntry.created_by == user_id,
            KnowledgeEntry.entry_type == "question",
            KnowledgeEntry.created_at >= today_start,
        )
        .scalar()
    ) or 0


def check_question_quota(db: Session, user_id: int) -> dict:
    """Return quota info: {used, limit, remaining, allowed}."""
    cap = get_question_cap(db)
    used = get_questions_asked_today(db, user_id)
    return {
        "used": used,
        "limit": cap,
        "remaining": max(0, cap - used),
        "allowed": used < cap,
    }


# ═══════════════════════════════════════════════════════════════════════
#  ADAPTIVE CARD BUILDERS
# ═══════════════════════════════════════════════════════════════════════


def _build_question_card_item(question: KnowledgeEntry, is_nudge: bool = False) -> list[dict]:
    """Build Adaptive Card body elements for a single question."""
    now = datetime.now(timezone.utc)
    age_hours = (now - question.created_at).total_seconds() / 3600 if question.created_at else 0

    elements = []

    # Urgency banner for nudge
    if is_nudge:
        elements.append({
            "type": "TextBlock",
            "text": "Awaiting your response ({:.0f}h)".format(age_hours),
            "color": "Attention",
            "weight": "Bolder",
            "size": "Small",
        })

    # Question context
    context_parts = []
    if question.requisition_id:
        context_parts.append("Req #{}".format(question.requisition_id))
    if question.mpn:
        context_parts.append("MPN: {}".format(question.mpn))
    creator_name = question.creator.display_name if question.creator else "Someone"
    context_parts.append("asked by {}".format(creator_name))

    elements.append({
        "type": "TextBlock",
        "text": " | ".join(context_parts),
        "size": "Small",
        "isSubtle": True,
    })

    # Question text
    elements.append({
        "type": "TextBlock",
        "text": question.content,
        "wrap": True,
    })

    # Answer text input
    elements.append({
        "type": "Input.Text",
        "id": "answer_{}".format(question.id),
        "placeholder": "Type your answer (or leave blank to skip)...",
        "isMultiline": True,
    })

    # Separator
    elements.append({"type": "TextBlock", "text": "---", "spacing": "Small"})

    return elements


def build_question_batch_card(questions: list[KnowledgeEntry]) -> dict:
    """Build an Adaptive Card containing multiple questions for batch answering."""
    body = [
        {
            "type": "TextBlock",
            "text": "You have {} question{} to review".format(len(questions), "s" if len(questions) != 1 else ""),
            "weight": "Bolder",
            "size": "Medium",
        },
    ]

    for q in questions:
        now = datetime.now(timezone.utc)
        age_hours = (now - q.created_at).total_seconds() / 3600 if q.created_at else 0
        is_nudge = age_hours >= 4
        body.extend(_build_question_card_item(q, is_nudge=is_nudge))

    # Submit action
    body.append({
        "type": "ActionSet",
        "actions": [
            {
                "type": "Action.Submit",
                "title": "Submit Answers",
                "data": {
                    "action": "submit_answers",
                    "question_ids": [q.id for q in questions],
                },
            }
        ],
    })

    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
    }


def build_digest_card(answered_questions: list[dict], pending_count: int) -> dict:
    """Build the daily knowledge digest Adaptive Card."""
    body = [
        {
            "type": "TextBlock",
            "text": "Knowledge Digest",
            "weight": "Bolder",
            "size": "Medium",
            "color": "Accent",
        },
    ]

    if pending_count > 0:
        body.append({
            "type": "TextBlock",
            "text": "You have {} unanswered question{} assigned to you.".format(
                pending_count, "s" if pending_count != 1 else ""
            ),
            "color": "Attention",
            "wrap": True,
        })

    if answered_questions:
        body.append({
            "type": "TextBlock",
            "text": "Answers to your questions (last 24h):",
            "weight": "Bolder",
            "size": "Small",
            "spacing": "Medium",
        })
        for aq in answered_questions[:10]:
            body.append({
                "type": "TextBlock",
                "text": "Q: {}".format(aq["question"][:100]),
                "wrap": True,
                "size": "Small",
                "isSubtle": True,
            })
            body.append({
                "type": "TextBlock",
                "text": "A: {}".format(aq["answer"][:200]),
                "wrap": True,
                "size": "Small",
            })

    if not answered_questions and pending_count == 0:
        # Should not happen (caller skips empty digests), but just in case
        body.append({"type": "TextBlock", "text": "No Q&A activity.", "isSubtle": True})

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
    """Send a batch Adaptive Card with all pending questions for this buyer.

    Returns count of questions delivered.
    """
    from app.models.auth import User
    from app.services.teams_notifications import send_teams_dm

    # Get undelivered, unresolved questions assigned to this user
    pending = (
        db.query(KnowledgeEntry)
        .filter(
            KnowledgeEntry.entry_type == "question",
            KnowledgeEntry.is_resolved.is_(False),
            KnowledgeEntry.delivered_at.is_(None),
            KnowledgeEntry.assigned_to_ids.cast(sa.Text).contains(str(user_id)),
        )
        .order_by(KnowledgeEntry.created_at.asc())
        .all()
    )

    if not pending:
        return 0

    user = db.get(User, user_id)
    if not user or not user.is_active:
        return 0

    card = build_question_batch_card(pending)

    # Send via Graph API DM as Adaptive Card
    try:
        await _send_adaptive_card_dm(user, card, db)
    except Exception as e:
        logger.warning("Failed to send question batch to user {}: {}", user_id, e)
        return 0

    # Mark all as delivered
    now = datetime.now(timezone.utc)
    for q in pending:
        q.delivered_at = now
        # Mark nudge if >4h old
        age_hours = (now - q.created_at).total_seconds() / 3600 if q.created_at else 0
        if age_hours >= 4 and not q.nudged_at:
            q.nudged_at = now
    db.commit()

    logger.info("Delivered {} questions to user {}", len(pending), user_id)
    return len(pending)


async def deliver_knowledge_digest(db: Session, user_id: int) -> bool:
    """Send the daily knowledge digest to a user.

    Contains: pending question count + answers received in last 24h.
    Returns True if sent, False if skipped (nothing to report).
    """
    from app.models.auth import User
    from app.services.teams_notifications import send_teams_dm

    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    # Count pending questions assigned to this user
    all_questions = (
        db.query(KnowledgeEntry)
        .filter(
            KnowledgeEntry.entry_type == "question",
            KnowledgeEntry.is_resolved.is_(False),
        )
        .all()
    )
    pending_count = sum(1 for q in all_questions if user_id in (q.assigned_to_ids or []))

    # Get questions asked by this user that got answered in last 24h
    answered_questions = []
    my_questions = (
        db.query(KnowledgeEntry)
        .filter(
            KnowledgeEntry.created_by == user_id,
            KnowledgeEntry.entry_type == "question",
            KnowledgeEntry.is_resolved.is_(True),
        )
        .all()
    )
    for q in my_questions:
        recent_answers = (
            db.query(KnowledgeEntry)
            .filter(
                KnowledgeEntry.parent_id == q.id,
                KnowledgeEntry.entry_type == "answer",
                KnowledgeEntry.created_at >= cutoff_24h,
            )
            .all()
        )
        for a in recent_answers:
            answered_questions.append({"question": q.content, "answer": a.content})

    # Skip empty digests
    if pending_count == 0 and not answered_questions:
        return False

    user = db.get(User, user_id)
    if not user or not user.is_active:
        return False

    card = build_digest_card(answered_questions, pending_count)

    try:
        await _send_adaptive_card_dm(user, card, db)
        logger.info("Sent knowledge digest to user {}", user_id)
        return True
    except Exception as e:
        logger.warning("Failed to send knowledge digest to user {}: {}", user_id, e)
        return False


# ═══════════════════════════════════════════════════════════════════════
#  CARD ACTION HANDLING
# ═══════════════════════════════════════════════════════════════════════


async def handle_card_answer(db: Session, payload: dict) -> dict:
    """Process an Adaptive Card answer submission from Teams.

    payload.data contains:
      - action: "submit_answers"
      - question_ids: [int, ...]
      - answer_<id>: "answer text" (for each question)

    Returns summary dict.
    """
    from app.services import knowledge_service

    data = payload.get("data", {})
    question_ids = data.get("question_ids", [])
    user_aad_id = payload.get("from", {}).get("aadObjectId", "")

    # Resolve user
    from app.services.teams_bot_service import _resolve_user
    user = _resolve_user(user_aad_id, db)
    if not user:
        return {"error": "User not found"}

    answered = 0
    for qid in question_ids:
        answer_text = data.get("answer_{}".format(qid), "").strip()
        if not answer_text:
            continue  # Skipped this question

        result = knowledge_service.post_answer(
            db,
            user_id=user.id,
            question_id=qid,
            content=answer_text,
        )
        if result:
            # Mark as answered via Teams
            result.answered_via = "teams"
            db.commit()
            answered += 1

    return {"answered": answered, "total": len(question_ids)}


# ═══════════════════════════════════════════════════════════════════════
#  GRAPH API ADAPTIVE CARD DM
# ═══════════════════════════════════════════════════════════════════════


async def _send_adaptive_card_dm(user, card: dict, db) -> None:
    """Send an Adaptive Card as a Teams DM via Graph API.

    Similar to send_teams_dm but sends an attachment instead of plain text.
    """
    if not user.access_token and not db:
        logger.debug("No token for %s, skipping card DM", user.email)
        return

    try:
        from app.utils.graph_client import GraphClient

        if db:
            from app.scheduler import get_valid_token
            token = await get_valid_token(user, db)
        else:
            token = user.access_token

        if not token:
            logger.debug("No valid token for %s, skipping card DM", user.email)
            return

        gc = GraphClient(token)

        # Create or get 1:1 chat
        chat = await gc.post_json(
            "/chats",
            {
                "chatType": "oneOnOne",
                "members": [
                    {
                        "@odata.type": "#microsoft.graph.aadUserConversationMember",
                        "roles": ["owner"],
                        "user@odata.bind": "https://graph.microsoft.com/v1.0/users/{}".format(user.email),
                    }
                ],
            },
        )
        chat_id = chat.get("id")
        if chat_id:
            await gc.post_json(
                "/chats/{}/messages".format(chat_id),
                {
                    "body": {"contentType": "html", "content": "You have questions to review."},
                    "attachments": [
                        {
                            "id": "card1",
                            "contentType": "application/vnd.microsoft.card.adaptive",
                            "content": card,
                        }
                    ],
                },
            )
            logger.info("Adaptive Card DM sent to %s", user.email)
    except Exception as e:
        logger.debug("Adaptive Card DM to %s failed: %s", user.email, e)
        raise
```

Note: The `deliver_question_batch` function has a JSON column query issue. Since `assigned_to_ids` is a JSON column storing a list, we need to use PostgreSQL JSON containment. Fix the filter in Step 2.

**Step 2: Fix the JSON column filter**

Replace the `pending` query filter line using `cast` with a PostgreSQL JSON containment check:

```python
from sqlalchemy.dialects.postgresql import array as pg_array
from sqlalchemy import text as sa_text

# Use raw SQL for JSON array containment (PostgreSQL-specific)
pending = (
    db.query(KnowledgeEntry)
    .filter(
        KnowledgeEntry.entry_type == "question",
        KnowledgeEntry.is_resolved.is_(False),
        KnowledgeEntry.delivered_at.is_(None),
        sa_text("assigned_to_ids::jsonb @> :uid_json").bindparams(uid_json="[{}]".format(user_id)),
    )
    .order_by(KnowledgeEntry.created_at.asc())
    .all()
)
```

Also add `import sqlalchemy as sa` at the top (remove the wrong `sa.Text` reference).

**Step 3: Commit**

```bash
git add app/services/teams_qa_service.py
git commit -m "feat(phase2): teams Q&A service — card builders, batch delivery, digest"
```

---

### Task 3: Question cap enforcement in knowledge_service

**Files:**
- Modify: `app/services/knowledge_service.py` (lines 134-172, `post_question` function)
- Modify: `app/routers/knowledge.py` (line 140-157, `post_question` endpoint)

**Step 1: Add cap check to post_question**

In `app/services/knowledge_service.py`, modify `post_question()` to add a cap check at the top of the function (after line 146):

```python
def post_question(
    db: Session,
    *,
    user_id: int,
    content: str,
    assigned_to_ids: list[int],
    mpn: str | None = None,
    vendor_card_id: int | None = None,
    company_id: int | None = None,
    requisition_id: int | None = None,
    requirement_id: int | None = None,
) -> KnowledgeEntry:
    """Post a Q&A question and notify assigned buyers."""
    # Check daily question cap
    from app.services.teams_qa_service import check_question_quota

    quota = check_question_quota(db, user_id)
    if not quota["allowed"]:
        raise ValueError("Daily question limit reached ({}/{})".format(quota["used"], quota["limit"]))

    entry = create_entry(
        # ... rest unchanged
```

**Step 2: Handle ValueError in the router**

In `app/routers/knowledge.py`, modify the `post_question` endpoint (around line 140) to catch the ValueError:

```python
@router.post("/question")
def post_question(
    payload: QuestionCreate,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    try:
        entry = knowledge_service.post_question(
            db,
            user_id=user.id,
            content=payload.content,
            assigned_to_ids=payload.assigned_to_ids,
            mpn=payload.mpn,
            vendor_card_id=payload.vendor_card_id,
            company_id=payload.company_id,
            requisition_id=payload.requisition_id,
            requirement_id=payload.requirement_id,
        )
        return _entry_to_response(entry)
    except ValueError as e:
        raise HTTPException(429, str(e))
```

**Step 3: Add quota endpoint**

In `app/routers/knowledge.py`, add after the delete endpoint (line 137):

```python
@router.get("/quota")
def get_quota(
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    from app.services.teams_qa_service import check_question_quota
    return check_question_quota(db, user.id)
```

**Step 4: Commit**

```bash
git add app/services/knowledge_service.py app/routers/knowledge.py
git commit -m "feat(phase2): daily question cap enforcement + quota endpoint"
```

---

### Task 4: Card action endpoint in teams_bot router

**Files:**
- Modify: `app/routers/teams_bot.py`

**Step 1: Add card-action endpoint**

In `app/routers/teams_bot.py`, add after the `/message` endpoint (after line 98):

```python
@router.post("/card-action")
async def handle_card_action(request: Request):
    """Handle Adaptive Card action submissions from Teams.

    Teams sends card action data when a user submits a form in an Adaptive Card.
    Validates HMAC, then routes to the appropriate handler.
    """
    config = _get_bot_config()

    # Validate HMAC if secret configured
    hmac_secret = config.get("teams_bot_hmac_secret", "")
    if hmac_secret:
        body = await request.body()
        auth = request.headers.get("Authorization", "")
        if not _validate_hmac(body, auth, hmac_secret):
            raise HTTPException(401, "Invalid HMAC signature")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    action = data.get("data", {}).get("action", "")
    if action != "submit_answers":
        raise HTTPException(400, "Unknown card action")

    from app.database import SessionLocal
    from app.services.teams_qa_service import handle_card_answer

    db = SessionLocal()
    try:
        result = await handle_card_answer(db, data)
        logger.info("Card action processed: %s", result)

        # Return confirmation card
        answered = result.get("answered", 0)
        return _card_response(
            "{} answer{} submitted. Thank you!".format(answered, "s" if answered != 1 else "")
            if answered > 0
            else "No answers provided."
        )
    finally:
        db.close()
```

**Step 2: Commit**

```bash
git add app/routers/teams_bot.py
git commit -m "feat(phase2): card-action endpoint for Adaptive Card submissions"
```

---

### Task 5: Background jobs — batch delivery + digest

**Files:**
- Modify: `app/jobs/knowledge_jobs.py`

**Step 1: Add the two new jobs**

In `app/jobs/knowledge_jobs.py`, add to `register_knowledge_jobs()` after the existing job registrations (after line 30):

```python
    scheduler.add_job(
        _job_deliver_question_batches,
        IntervalTrigger(hours=1),
        id="knowledge_deliver_batches",
        name="Deliver batched Q&A questions to buyers",
    )
    scheduler.add_job(
        _job_send_knowledge_digests,
        IntervalTrigger(hours=1),
        id="knowledge_send_digests",
        name="Send daily knowledge digests",
    )
```

**Step 2: Implement the batch delivery job**

Add after the existing `_job_expire_stale` function:

```python
async def _job_deliver_question_batches():
    """Deliver batched question cards to buyers whose digest hour matches now.

    Runs every hour. Checks each user with TeamsAlertConfig — if current UTC hour
    matches their knowledge_digest_hour or knowledge_digest_hour + 6, send batch.
    """
    from app.database import SessionLocal
    from app.models.teams_alert_config import TeamsAlertConfig
    from app.services.teams_qa_service import deliver_question_batch

    db = SessionLocal()
    try:
        current_hour = datetime.now(timezone.utc).hour
        configs = db.query(TeamsAlertConfig).filter(TeamsAlertConfig.alerts_enabled.is_(True)).all()

        delivered_total = 0
        for config in configs:
            digest_hour = config.knowledge_digest_hour or 14
            # Deliver at digest_hour and digest_hour + 6
            if current_hour not in (digest_hour % 24, (digest_hour + 6) % 24):
                continue
            try:
                count = await deliver_question_batch(db, config.user_id)
                delivered_total += count
            except Exception as e:
                logger.warning("Batch delivery failed for user {}: {}", config.user_id, e)

        if delivered_total:
            logger.info("Delivered {} questions across batch runs", delivered_total)
    except Exception as e:
        logger.error("deliver_question_batches job failed: {}", e)
    finally:
        db.close()


async def _job_send_knowledge_digests():
    """Send daily knowledge digests to users whose digest hour matches now.

    Runs every hour. Only sends at the user's configured knowledge_digest_hour.
    """
    from app.database import SessionLocal
    from app.models.teams_alert_config import TeamsAlertConfig
    from app.services.teams_qa_service import deliver_knowledge_digest

    db = SessionLocal()
    try:
        current_hour = datetime.now(timezone.utc).hour
        configs = db.query(TeamsAlertConfig).filter(TeamsAlertConfig.alerts_enabled.is_(True)).all()

        sent_count = 0
        for config in configs:
            digest_hour = config.knowledge_digest_hour or 14
            if current_hour != digest_hour % 24:
                continue
            try:
                sent = await deliver_knowledge_digest(db, config.user_id)
                if sent:
                    sent_count += 1
            except Exception as e:
                logger.warning("Digest delivery failed for user {}: {}", config.user_id, e)

        if sent_count:
            logger.info("Sent {} knowledge digests", sent_count)
    except Exception as e:
        logger.error("send_knowledge_digests job failed: {}", e)
    finally:
        db.close()
```

**Step 3: Commit**

```bash
git add app/jobs/knowledge_jobs.py
git commit -m "feat(phase2): batch delivery + digest background jobs"
```

---

### Task 6: Admin config endpoint

**Files:**
- Modify: `app/routers/knowledge.py`

**Step 1: Add admin config endpoint**

In `app/routers/knowledge.py`, add after the quota endpoint:

```python
@router.get("/config")
def get_knowledge_config(
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Get knowledge config values (admin only)."""
    from app.models.knowledge import KnowledgeConfig
    rows = db.query(KnowledgeConfig).all()
    return {row.key: row.value for row in rows}


@router.put("/config")
def update_knowledge_config(
    payload: dict,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Update knowledge config (admin only). Body: {key: value, ...}."""
    from app.models.knowledge import KnowledgeConfig

    # Check admin
    from app.config import settings
    if user.email not in (settings.ADMIN_EMAILS or "").split(","):
        raise HTTPException(403, "Admin only")

    for key, value in payload.items():
        row = db.query(KnowledgeConfig).filter(KnowledgeConfig.key == key).first()
        if row:
            row.value = str(value)
        else:
            db.add(KnowledgeConfig(key=key, value=str(value)))
    db.commit()
    return {"ok": True}
```

**Step 2: Commit**

```bash
git add app/routers/knowledge.py
git commit -m "feat(phase2): admin config endpoints for question cap"
```

---

### Task 7: Frontend — quota display in question modal

**Files:**
- Modify: `app/static/app.js` (lines 3823-3890, `_openAskQuestionModal` function)

**Step 1: Add quota fetch and display**

In `app/static/app.js`, modify `_openAskQuestionModal` to fetch quota and display it. After the `buyers` fetch (line 3828), add:

```javascript
    var quota = { used: 0, limit: 10, remaining: 10, allowed: true };
    try {
        quota = await apiFetch('/api/knowledge/quota');
    } catch (e) { /* fallback: no limit shown */ }
```

After the `selectWrap` is appended to `box` (line 3871), add the quota display:

```javascript
    var quotaDiv = document.createElement('div');
    quotaDiv.style.cssText = 'margin-top:8px;font-size:11px;color:var(--muted)';
    if (quota.allowed) {
        quotaDiv.textContent = quota.remaining + '/' + quota.limit + ' questions remaining today';
    } else {
        quotaDiv.textContent = 'Daily question limit reached (' + quota.limit + '/' + quota.limit + '). Try again tomorrow.';
        quotaDiv.style.color = 'var(--danger, #e74c3c)';
    }
    box.appendChild(quotaDiv);
```

Also, if quota is not allowed, disable the submit button. After the submit button creation (line 3882):

```javascript
    if (!quota.allowed) {
        submitBtn.disabled = true;
        submitBtn.style.opacity = '0.5';
    }
```

**Step 2: Commit**

```bash
git add app/static/app.js
git commit -m "feat(phase2): quota display in question modal"
```

---

### Task 8: Mark answer source in post_answer

**Files:**
- Modify: `app/services/knowledge_service.py` (lines 175-218, `post_answer` function)

**Step 1: Add answered_via parameter**

Modify the `post_answer` function signature and pass through `answered_via`:

```python
def post_answer(
    db: Session,
    *,
    user_id: int,
    question_id: int,
    content: str,
    answered_via: str = "web",
) -> KnowledgeEntry | None:
    """Answer a question. Marks question resolved and notifies asker."""
    question = db.get(KnowledgeEntry, question_id)
    if not question or question.entry_type != "question":
        return None

    answer = create_entry(
        db,
        user_id=user_id,
        entry_type="answer",
        content=content,
        source="manual",
        parent_id=question_id,
        mpn=question.mpn,
        vendor_card_id=question.vendor_card_id,
        company_id=question.company_id,
        requisition_id=question.requisition_id,
        requirement_id=question.requirement_id,
    )

    # Set answer source
    answer.answered_via = answered_via

    # Mark question as resolved
    question.is_resolved = True
    db.commit()
    # ... rest of notification code unchanged
```

**Step 2: Commit**

```bash
git add app/services/knowledge_service.py
git commit -m "feat(phase2): answered_via tracking on answers"
```

---

### Task 9: Rebuild and deploy

**Step 1: Rebuild Docker**

```bash
docker compose up -d --build
```

**Step 2: Check logs for clean startup**

```bash
docker compose logs -f app 2>&1 | head -50
```

**Step 3: Run migration inside Docker**

```bash
docker compose exec app alembic upgrade head
```

**Step 4: Verify endpoints**

```bash
# Check quota endpoint
curl -s http://localhost:8000/api/knowledge/quota -H "Cookie: session=..." | python3 -m json.tool

# Check config endpoint
curl -s http://localhost:8000/api/knowledge/config -H "Cookie: session=..." | python3 -m json.tool
```

**Step 5: Commit and push**

```bash
git add -A
git commit -m "feat(phase2): Teams Q&A routing + daily digest — complete"
git push origin main
```
