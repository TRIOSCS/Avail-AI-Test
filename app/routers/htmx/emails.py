"""routers/htmx/emails.py — Email thread views and reply for HTMX frontend.

Surfaces email threads linked to requisitions and vendors,
inline reply, AI thread summaries, and email intelligence dashboard.

Called by: htmx router package
Depends on: services.email_threads, dependencies.require_fresh_token,
            utils.graph_client
"""

from html import escape

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import require_fresh_token, require_user
from ...models import User
from ...services import email_threads
from ._helpers import router

# ═══════════════════════════════════════════════════════════════════════
#  HTML rendering helpers
# ═══════════════════════════════════════════════════════════════════════


def _error_html(message: str) -> str:
    """Render a friendly error banner for HTMX partials."""
    safe = escape(message)
    return (
        '<div class="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">'
        f'<p>{safe}</p>'
        "</div>"
    )


def _empty_html(message: str) -> str:
    """Render an empty-state placeholder."""
    safe = escape(message)
    return (
        '<div class="text-center text-gray-400 py-8">'
        f"{safe}"
        "</div>"
    )


def _thread_row(thread: dict) -> str:
    """Render a single email thread as an HTML table row."""
    subject = escape(thread.get("subject", "(No Subject)"))
    participants = ", ".join(thread.get("participants", []))
    safe_participants = escape(participants or "Unknown")
    date_str = escape(thread.get("last_message_date", "")[:10] if thread.get("last_message_date") else "")
    count = thread.get("message_count", 0)
    conv_id = escape(thread.get("conversation_id", ""))
    needs_response = thread.get("needs_response", False)
    badge = (
        ' <span class="ml-1 inline-flex items-center rounded-full bg-amber-100 '
        'px-2 py-0.5 text-xs text-amber-700">Needs reply</span>'
        if needs_response
        else ""
    )
    return (
        f'<tr class="hover:bg-brand-50 cursor-pointer"'
        f' hx-get="/v2/partials/emails/thread/{conv_id}"'
        f' hx-target="#email-thread-detail" hx-swap="innerHTML">'
        f'<td class="px-4 py-2 text-sm font-medium">{subject}{badge}</td>'
        f'<td class="px-4 py-2 text-sm text-gray-500">{safe_participants}</td>'
        f'<td class="px-4 py-2 text-sm text-gray-500">{date_str}</td>'
        f'<td class="px-4 py-2 text-sm text-gray-500 text-center">{count}</td>'
        f"</tr>"
    )


def _threads_table(threads: list[dict], empty_msg: str = "No email threads found") -> str:
    """Wrap thread rows in a full HTML table, or show empty state."""
    if not threads:
        return _empty_html(empty_msg)
    rows = "\n".join(_thread_row(t) for t in threads)
    return (
        '<table class="w-full text-left">'
        "<thead>"
        '<tr class="border-b">'
        '<th class="px-4 py-2 text-xs text-gray-500 uppercase">Subject</th>'
        '<th class="px-4 py-2 text-xs text-gray-500 uppercase">From</th>'
        '<th class="px-4 py-2 text-xs text-gray-500 uppercase">Date</th>'
        '<th class="px-4 py-2 text-xs text-gray-500 uppercase text-center">Messages</th>'
        "</tr>"
        "</thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
        '<div id="email-thread-detail"></div>'
    )


def _message_bubble(msg: dict) -> str:
    """Render a single email message as a chat-style bubble."""
    direction = msg.get("direction", "received")
    from_name = escape(msg.get("from_name", "") or msg.get("from_email", "Unknown"))
    from_email = escape(msg.get("from_email", ""))
    date_str = escape((msg.get("received_date", "") or "")[:16].replace("T", " "))
    snippet = escape(msg.get("body_preview", ""))
    msg_id = escape(msg.get("id", ""))

    if direction == "sent":
        align = "ml-auto bg-blue-50 border-blue-200"
        label = "You"
    else:
        align = "mr-auto bg-gray-50 border-gray-200"
        label = from_name

    return (
        f'<div class="max-w-[80%] {align} rounded-lg border p-3 mb-3" data-message-id="{msg_id}">'
        f'<div class="flex justify-between items-center mb-1">'
        f'<span class="text-sm font-medium">{escape(label)}</span>'
        f'<span class="text-xs text-gray-400">{date_str}</span>'
        f"</div>"
        f'<p class="text-sm text-gray-700">{snippet}</p>'
        f'<div class="text-xs text-gray-400 mt-1">{from_email}</div>'
        f"</div>"
    )


def _toast_html(message: str, variant: str = "success") -> str:
    """Render an inline toast notification."""
    colors = {
        "success": "border-green-200 bg-green-50 text-green-700",
        "error": "border-red-200 bg-red-50 text-red-700",
    }
    cls = colors.get(variant, colors["success"])
    safe = escape(message)
    return (
        f'<div class="rounded-lg border {cls} p-3 text-sm" '
        f'hx-swap-oob="true" id="email-toast">'
        f"{safe}"
        f"</div>"
    )


# ═══════════════════════════════════════════════════════════════════════
#  1. Requirement email threads tab
# ═══════════════════════════════════════════════════════════════════════


@router.get("/v2/partials/requisitions/{req_id}/tab/emails", response_class=HTMLResponse)
async def requirement_emails_tab(
    req_id: int,
    request: Request,
    user: User = Depends(require_user),
    token: str = Depends(require_fresh_token),
    db: Session = Depends(get_db),
):
    """Show email threads linked to a requirement."""
    try:
        threads = await email_threads.fetch_threads_for_requirement(
            req_id, token, db, user_id=user.id
        )
        return HTMLResponse(_threads_table(threads))
    except (ConnectionError, TimeoutError, OSError, RuntimeError) as exc:
        logger.error(f"Failed to fetch email threads for requirement {req_id}: {exc}")
        return HTMLResponse(_error_html("Could not load emails \u2014 please try again later."))


# ═══════════════════════════════════════════════════════════════════════
#  2. Full thread view
# ═══════════════════════════════════════════════════════════════════════


@router.get("/v2/partials/emails/thread/{conversation_id}", response_class=HTMLResponse)
async def thread_messages_partial(
    conversation_id: str,
    request: Request,
    user: User = Depends(require_user),
    token: str = Depends(require_fresh_token),
    db: Session = Depends(get_db),
):
    """Show all messages in a conversation thread as chat bubbles."""
    try:
        messages = await email_threads.fetch_thread_messages(conversation_id, token)
    except (ConnectionError, TimeoutError, OSError, RuntimeError) as exc:
        logger.error(f"Failed to fetch thread messages: {exc}")
        return HTMLResponse(_error_html("Could not load thread messages."))

    if not messages:
        return HTMLResponse(_empty_html("No messages in this thread."))

    bubbles = "\n".join(_message_bubble(m) for m in messages)

    # Determine reply target from last received message
    last_received = None
    for m in reversed(messages):
        if m.get("direction") == "received":
            last_received = m
            break
    last_received = last_received or messages[-1]
    last_msg_id = escape(last_received.get("id", ""))
    last_from = escape(last_received.get("from_email", ""))
    safe_conv_id = escape(conversation_id)

    reply_form = (
        f'<form hx-post="/v2/partials/emails/reply" hx-target="#email-toast" '
        f'hx-swap="outerHTML" class="mt-4 border-t pt-4">'
        f'<input type="hidden" name="conversation_id" value="{safe_conv_id}">'
        f'<input type="hidden" name="message_id" value="{last_msg_id}">'
        f'<input type="hidden" name="to_email" value="{last_from}">'
        f'<textarea name="body" rows="3" placeholder="Type your reply\u2026"'
        f' class="w-full rounded border border-gray-300 p-2 text-sm'
        f' focus:border-blue-500 focus:ring-1 focus:ring-blue-500"></textarea>'
        f'<button type="submit" class="mt-2 rounded bg-blue-600 px-4 py-2'
        f' text-sm font-medium text-white hover:bg-blue-700">Send Reply</button>'
        f"</form>"
        f'<div id="email-toast"></div>'
    )

    summary_btn = (
        f'<button hx-get="/v2/partials/emails/thread/{safe_conv_id}/summary"'
        f' hx-target="#thread-summary" hx-swap="innerHTML"'
        f' class="mb-3 text-sm text-blue-600 hover:text-blue-800 underline">'
        f"Summarize with AI"
        f"</button>"
        f'<div id="thread-summary"></div>'
    )

    html = (
        f'<div class="space-y-1">'
        f"{summary_btn}"
        f'<div class="space-y-0">{bubbles}</div>'
        f"{reply_form}"
        f"</div>"
    )
    return HTMLResponse(html)


# ═══════════════════════════════════════════════════════════════════════
#  3. Reply to a thread
# ═══════════════════════════════════════════════════════════════════════


@router.post("/v2/partials/emails/reply", response_class=HTMLResponse)
async def send_reply_partial(
    request: Request,
    conversation_id: str = Form(...),
    message_id: str = Form(...),
    body: str = Form(...),
    to_email: str = Form(...),
    user: User = Depends(require_user),
    token: str = Depends(require_fresh_token),
    db: Session = Depends(get_db),
):
    """Send a reply in an existing email thread via Graph API."""
    from ...email_service import _build_html_body
    from ...utils.graph_client import GraphClient

    gc = GraphClient(token)
    html_body = _build_html_body(body)

    mail_payload = {
        "message": {
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": to_email}}],
        },
        "saveToSentItems": "true",
    }

    try:
        result = await gc.post_json("/me/sendMail", mail_payload)
        if isinstance(result, dict) and "error" in result:
            logger.error(f"Graph reply error: {result}")
            return HTMLResponse(_toast_html("Failed to send reply. Please try again.", "error"))
    except (ConnectionError, TimeoutError, OSError, RuntimeError) as exc:
        logger.error(f"Reply send failed: {exc}")
        return HTMLResponse(_toast_html(f"Failed to send reply: {str(exc)[:100]}", "error"))

    # Invalidate thread cache so next load picks up the reply
    email_threads.clear_cache()

    return HTMLResponse(_toast_html(f"Reply sent to {escape(to_email)}"))


# ═══════════════════════════════════════════════════════════════════════
#  4. Vendor email threads
# ═══════════════════════════════════════════════════════════════════════


@router.get("/v2/partials/vendors/{vendor_id}/emails", response_class=HTMLResponse)
async def vendor_emails_partial(
    vendor_id: int,
    request: Request,
    user: User = Depends(require_user),
    token: str = Depends(require_fresh_token),
    db: Session = Depends(get_db),
):
    """Show email threads for a vendor."""
    try:
        threads = await email_threads.fetch_threads_for_vendor(
            vendor_id, token, db, user_id=user.id
        )
        return HTMLResponse(_threads_table(threads, empty_msg="No email threads with this vendor."))
    except (ConnectionError, TimeoutError, OSError, RuntimeError) as exc:
        logger.error(f"Failed to fetch vendor {vendor_id} email threads: {exc}")
        return HTMLResponse(_error_html("Could not load vendor emails \u2014 please try again."))


# ═══════════════════════════════════════════════════════════════════════
#  5. AI thread summary
# ═══════════════════════════════════════════════════════════════════════


@router.get("/v2/partials/emails/thread/{conversation_id}/summary", response_class=HTMLResponse)
async def thread_summary_partial(
    conversation_id: str,
    request: Request,
    user: User = Depends(require_user),
    token: str = Depends(require_fresh_token),
    db: Session = Depends(get_db),
):
    """Get AI-generated summary of an email thread, rendered as an HTML card."""
    from ...services.email_intelligence_service import summarize_thread

    try:
        summary = await summarize_thread(token, conversation_id, db, user.id)
    except (ConnectionError, TimeoutError, OSError, RuntimeError, Exception) as exc:
        logger.error(f"Thread summary failed for {conversation_id[:20]}: {exc}")
        return HTMLResponse(_error_html("Could not generate summary. Please try again."))

    if not summary:
        return HTMLResponse(
            '<div class="rounded-lg border border-gray-200 bg-gray-50 p-4 text-sm text-gray-500">'
            "Could not generate a summary for this thread."
            "</div>"
        )

    # summary is a dict with keys like 'summary', 'action_items', 'sentiment'
    summary_text = escape(summary.get("summary", str(summary)) if isinstance(summary, dict) else str(summary))
    action_items = summary.get("action_items", []) if isinstance(summary, dict) else []
    sentiment = escape(summary.get("sentiment", "") if isinstance(summary, dict) else "")

    action_html = ""
    if action_items:
        items = "".join(f"<li>{escape(str(item))}</li>" for item in action_items)
        action_html = (
            '<div class="mt-2">'
            '<span class="text-xs font-semibold text-gray-500 uppercase">Action Items</span>'
            f'<ul class="list-disc list-inside text-sm text-gray-700 mt-1">{items}</ul>'
            "</div>"
        )

    sentiment_html = ""
    if sentiment:
        sentiment_html = (
            f'<div class="mt-2 text-xs text-gray-400">Sentiment: {sentiment}</div>'
        )

    return HTMLResponse(
        '<div class="rounded-lg border border-blue-200 bg-blue-50 p-4 mb-3">'
        '<div class="flex items-center gap-2 mb-2">'
        '<span class="text-xs font-semibold text-blue-700 uppercase">AI Summary</span>'
        "</div>"
        f'<p class="text-sm text-gray-700">{summary_text}</p>'
        f"{action_html}"
        f"{sentiment_html}"
        "</div>"
    )


# ═══════════════════════════════════════════════════════════════════════
#  6. Email intelligence dashboard
# ═══════════════════════════════════════════════════════════════════════


@router.get("/v2/partials/email-intelligence", response_class=HTMLResponse)
async def email_intelligence_partial(
    request: Request,
    days: int = 7,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render email intelligence dashboard as an HTML partial."""
    from ...services.response_analytics import get_email_intelligence_dashboard

    try:
        data = get_email_intelligence_dashboard(db, user.id, days=min(days, 90))
    except (Exception,) as exc:
        logger.error(f"Email intelligence dashboard error: {exc}")
        return HTMLResponse(_error_html("Could not load email intelligence data."))

    scanned = data.get("emails_scanned_7d", 0)
    offers = data.get("offers_detected_7d", 0)
    stock_lists = data.get("stock_lists_7d", 0)
    pending = data.get("pending_review", 0)
    avg_response = data.get("avg_response_hours")
    response_rate = data.get("response_rate")

    avg_resp_str = f"{avg_response:.1f}h" if avg_response is not None else "N/A"
    resp_rate_str = f"{response_rate:.0%}" if response_rate is not None else "N/A"

    def _stat_card(label: str, value: str, color: str = "blue") -> str:
        return (
            f'<div class="rounded-lg border border-{color}-200 bg-{color}-50 p-4 text-center">'
            f'<div class="text-2xl font-bold text-{color}-700">{escape(str(value))}</div>'
            f'<div class="text-xs text-gray-500 mt-1">{escape(label)}</div>'
            f"</div>"
        )

    cards = (
        f'<div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">'
        f'{_stat_card("Emails Scanned", str(scanned))}'
        f'{_stat_card("Offers Detected", str(offers), "green")}'
        f'{_stat_card("Stock Lists", str(stock_lists), "purple")}'
        f'{_stat_card("Pending Review", str(pending), "amber")}'
        f'{_stat_card("Avg Response", avg_resp_str, "gray")}'
        f'{_stat_card("Response Rate", resp_rate_str, "gray")}'
        f"</div>"
    )

    # Top vendors table
    top_vendors = data.get("top_vendors", [])
    vendor_rows = ""
    for v in top_vendors[:10]:
        vname = escape(str(v.get("vendor_name", "")))
        score = v.get("email_health_score", 0)
        vrate = v.get("response_rate")
        vrate_str = f"{vrate:.0%}" if vrate is not None else "N/A"
        vendor_rows += (
            f'<tr class="hover:bg-brand-50">'
            f'<td class="px-4 py-2 text-sm">{vname}</td>'
            f'<td class="px-4 py-2 text-sm text-center">{score}</td>'
            f'<td class="px-4 py-2 text-sm text-center">{vrate_str}</td>'
            f"</tr>"
        )

    vendor_table = ""
    if vendor_rows:
        vendor_table = (
            '<div class="mt-6">'
            '<h3 class="text-sm font-semibold text-gray-700 mb-2">Top Vendors by Email Health</h3>'
            '<table class="w-full text-left">'
            "<thead>"
            '<tr class="border-b">'
            '<th class="px-4 py-2 text-xs text-gray-500 uppercase">Vendor</th>'
            '<th class="px-4 py-2 text-xs text-gray-500 uppercase text-center">Health Score</th>'
            '<th class="px-4 py-2 text-xs text-gray-500 uppercase text-center">Response Rate</th>'
            "</tr>"
            "</thead>"
            f"<tbody>{vendor_rows}</tbody>"
            "</table>"
            "</div>"
        )

    return HTMLResponse(
        f'<div class="space-y-4">'
        f'<h2 class="text-lg font-semibold text-gray-800">Email Intelligence ({days}d)</h2>'
        f"{cards}"
        f"{vendor_table}"
        f"</div>"
    )
