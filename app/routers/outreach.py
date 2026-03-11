"""Outreach router — send ad-hoc sales/prospecting emails via Graph API.

Called by: frontend outreach UI, API clients
Depends on: GraphClient, dependencies (auth, token)
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_fresh_token, require_user
from app.email_service import _build_html_body
from app.models import ActivityLog, User
from app.utils.graph_client import GraphClient

router = APIRouter(prefix="/api/outreach", tags=["outreach"])


class OutreachRecipient(BaseModel):
    name: str
    email: str
    company: str = ""


class OutreachRequest(BaseModel):
    recipients: list[OutreachRecipient]
    subject: str
    body: str


class OutreachResult(BaseModel):
    sent: list[dict]
    failed: list[dict]


@router.post("/send", response_model=OutreachResult)
async def send_outreach(
    req: OutreachRequest,
    user: User = Depends(require_user),
    token: str = Depends(require_fresh_token),
    db: Session = Depends(get_db),
):
    """Send an outreach email to one or more recipients."""
    if not req.recipients:
        raise HTTPException(400, "No recipients provided")
    if len(req.recipients) > 50:
        raise HTTPException(400, "Maximum 50 recipients per batch")

    gc = GraphClient(token)
    sent = []
    failed = []

    for r in req.recipients:
        # Personalise greeting: replace generic "Hi" / "Hi," with "Hi {name},"
        body_text = req.body
        if r.name:
            for greeting in ["Hi,", "Hi\n", "Hello,", "Hello\n"]:
                if body_text.startswith(greeting):
                    body_text = f"Hi {r.name.split()[0]}," + body_text[len(greeting) - 1 :]
                    break

        html_body = _build_html_body(body_text)
        payload = {
            "message": {
                "subject": req.subject,
                "body": {"contentType": "HTML", "content": html_body},
                "toRecipients": [{"emailAddress": {"address": r.email, "name": r.name}}],
                "isReadReceiptRequested": False,
                "isDeliveryReceiptRequested": False,
            },
            "saveToSentItems": "true",
        }

        try:
            result = await gc.post_json("/me/sendMail", payload)
            if isinstance(result, dict) and result.get("error"):
                raise Exception(f"Graph error {result['error']}: {result.get('detail', '')}")
            sent.append({"name": r.name, "email": r.email, "company": r.company})
            logger.info(f"Outreach sent to {r.email} ({r.company})")
        except Exception as e:
            logger.error(f"Outreach failed for {r.email}: {e}")
            failed.append({"name": r.name, "email": r.email, "company": r.company, "error": str(e)[:200]})

    # Log activity
    db.add(
        ActivityLog(
            user_id=user.id,
            activity_type="outreach",
            channel="email",
            summary=f"Sent to {len(sent)}/{len(req.recipients)} recipients: {req.subject[:100]}",
            created_at=datetime.now(timezone.utc),
        )
    )
    db.commit()

    return OutreachResult(sent=sent, failed=failed)
