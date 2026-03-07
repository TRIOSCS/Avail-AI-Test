"""Teams card action webhook — handles Action.Submit callbacks from Adaptive Cards.

When users click "Approve" or "Reject" on a buy plan card in Teams, Teams POSTs
the action data here. We validate, execute the action, and return a response card.

Called by: Microsoft Teams (Action.Submit on Adaptive Cards)
Depends on: app/models, app/services/buyplan_service, app/dependencies
"""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

from ..database import SessionLocal

router = APIRouter(prefix="/api/teams", tags=["teams"])


class CardActionPayload(BaseModel):
    action: str
    plan_id: int | None = None


@router.post("/card-action")
async def handle_card_action(request: Request):
    """Handle Action.Submit callbacks from Teams Adaptive Cards.

    Teams sends the action data as JSON. We parse and dispatch to the
    appropriate handler. Returns an Adaptive Card response shown in-place.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON payload")

    # Teams wraps action data in "value" when using Action.Submit
    action_data = body.get("value") or body
    action = action_data.get("action", "")
    plan_id = action_data.get("plan_id")

    if action in ("buyplan_approve", "buyplan_reject") and plan_id:
        return await _handle_buyplan_action(plan_id, action)
    else:
        logger.warning("Unknown Teams card action: %s", action)
        return _response_card("Unknown Action", "This action is not recognized.", "warning")


async def _handle_buyplan_action(plan_id: int, action: str) -> dict:
    """Approve or reject a buy plan from Teams card action."""
    is_approve = action == "buyplan_approve"
    verb = "approved" if is_approve else "rejected"
    color = "good" if is_approve else "attention"

    db = SessionLocal()
    try:
        from ..models.quotes import BuyPlan

        plan = db.get(BuyPlan, plan_id)
        if not plan:
            return _response_card("Not Found", f"Buy plan #{plan_id} not found.", "attention")
        if plan.status != "pending":
            return _response_card("Already Processed", f"Buy plan #{plan_id} is already {plan.status}.", "warning")

        plan.status = verb
        if is_approve:
            plan.approved_at = datetime.now(timezone.utc)
        else:
            plan.rejection_reason = "Rejected via Teams card"
        db.commit()
        logger.info("Buy plan #{} {} via Teams card action", plan_id, verb)
        return _response_card(verb.title(), f"Buy plan #{plan_id} has been {verb} via Teams.", color)
    except Exception as e:
        logger.error("Teams card action %s failed: %s", verb, e)
        db.rollback()
        return _response_card("Error", f"Failed to {verb[:-1]}: {str(e)[:200]}", "attention")
    finally:
        db.close()


def _response_card(title: str, message: str, color: str = "accent") -> dict:
    """Build a simple Adaptive Card response for Teams."""
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {"type": "TextBlock", "text": title, "weight": "Bolder", "color": color},
                        {"type": "TextBlock", "text": message, "wrap": True},
                    ],
                },
            }
        ],
    }
