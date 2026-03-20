# Excess Resell Phase 4: Email Bid Solicitations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bundled email sending, AI email polish, sent-message ID capture, and auto-parsing of bid reply emails to the excess resell module.

**Architecture:** Extend existing `send_bid_solicitation()` with bundled mode + `_find_sent_message()` lookup. Add `_handle_excess_bid_reply()` in `email_service.py` hooked into `poll_inbox()`. New `POST /api/excess-lists/polish-email` endpoint for AI cleanup. New `solicit_modal.html` template.

**Tech Stack:** FastAPI, SQLAlchemy, Microsoft Graph API, Anthropic Claude (via `claude_structured`), HTMX + Alpine.js, Jinja2

**Spec:** `docs/superpowers/specs/2026-03-20-excess-resell-phase4-solicitations-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `app/schemas/excess.py` | Modify | Add `bundled` field to `SendBidSolicitationRequest`, add `PolishEmailRequest`/`PolishEmailResponse` |
| `app/services/excess_service.py` | Modify | Add bundled mode to `send_bid_solicitation()`, add `_build_bundled_solicitation_html()` |
| `app/routers/excess.py` | Modify | Add `POST /api/excess-lists/polish-email` endpoint, forward `bundled` param to service |
| `app/email_service.py` | Modify | Add `_EXCESS_BID_RE`, `_handle_excess_bid_reply()`, hook into `poll_inbox()` |
| `app/templates/htmx/partials/excess/solicit_modal.html` | Create | Solicitation form modal with recipient picker, message textarea, AI clean up button, parts table |
| `app/templates/htmx/partials/excess/detail.html` | Modify | Add "Solicit Bids" button |
| `tests/test_excess_solicitations.py` | Create | All tests for send, polish, parse |

---

### Task 1: Add bundled field to schema

**Files:**
- Modify: `app/schemas/excess.py:260-268`

- [ ] **Step 1: Add bundled field and polish schemas**

In `app/schemas/excess.py`, add `bundled` to `SendBidSolicitationRequest` and add polish schemas:

```python
class SendBidSolicitationRequest(BaseModel):
    """Request body for sending a bid solicitation email."""

    line_item_ids: list[int] = Field(min_length=1)
    recipient_email: str
    recipient_name: str | None = None
    contact_id: int
    subject: str | None = None
    message: str | None = None
    bundled: bool = True  # True = one email with all items, False = separate emails


class PolishEmailRequest(BaseModel):
    """Request body for AI email polish."""
    text: str = Field(min_length=1, max_length=5000)


class PolishEmailResponse(BaseModel):
    """Response from AI email polish."""
    text: str
```

- [ ] **Step 2: Commit**

```bash
git add app/schemas/excess.py
git commit -m "feat(excess): add bundled field and polish email schemas"
```

---

### Task 2: Write send tests (bundled + split + sent-message lookup)

**Files:**
- Create: `tests/test_excess_solicitations.py`

- [ ] **Step 1: Write failing tests for bundled send, split send, and sent-message lookup**

```python
"""Tests for excess resell email bid solicitations (Phase 4).

Tests: send (bundled/split), AI polish, inbox parsing.
Depends on: app/services/excess_service, app/routers/excess, app/email_service
"""

import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# Uses `client` and `db_session` fixtures from tests/conftest.py.
# `client` provides a TestClient with auth overrides (get_db, require_user,
# require_fresh_token all mocked). No custom fixture needed.


@pytest.fixture
def excess_list_with_items(db_session):
    """Create an ExcessList with 3 line items for testing."""
    from app.models.excess import ExcessList, ExcessLineItem

    el = ExcessList(
        title="Test Excess", status="active", owner_id=1,
        company_id=1, total_line_items=3,
    )
    db_session.add(el)
    db_session.flush()
    items = []
    for i, (mpn, qty, price) in enumerate([
        ("LM358N", 5000, 0.42), ("SN74HC595N", 2000, 0.85), ("NE555P", 1000, 0.30),
    ]):
        item = ExcessLineItem(
            excess_list_id=el.id, part_number=mpn,
            quantity=qty, asking_price=price, status="available",
        )
        db_session.add(item)
        items.append(item)
    db_session.flush()
    return el, items


class TestSendBundled:
    """Bundled mode: one email with all items, one BidSolicitation per item."""

    @patch("app.services.excess_service.GraphClient")
    def test_bundled_creates_solicitations_per_item(
        self, mock_gc_cls, client, excess_list_with_items, db_session,
    ):
        el, items = excess_list_with_items
        mock_gc = AsyncMock()
        mock_gc.post_json = AsyncMock(return_value=None)  # sendMail returns 202 no body
        mock_gc.get_json = AsyncMock(return_value={
            "value": [{"id": "graph-msg-1", "conversationId": "conv-1", "subject": "test"}]
        })
        mock_gc_cls.return_value = mock_gc

        resp = client.post(f"/api/excess-lists/{el.id}/solicitations", json={
            "line_item_ids": [i.id for i in items],
            "recipient_email": "buyer@acme.com",
            "recipient_name": "John",
            "contact_id": 0,
            "bundled": True,
            "message": "We have parts available.",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["total"] == 3
        # All should share the same graph_message_id
        graph_ids = {s["graph_message_id"] for s in data["items"] if s.get("graph_message_id")}
        assert len(graph_ids) <= 1  # all same or all None
        # Only one sendMail call for bundled
        assert mock_gc.post_json.call_count == 1

    @patch("app.services.excess_service.GraphClient")
    def test_bundled_subject_uses_first_solicitation_id(
        self, mock_gc_cls, client, excess_list_with_items, db_session,
    ):
        el, items = excess_list_with_items
        mock_gc = AsyncMock()
        mock_gc.post_json = AsyncMock(return_value=None)
        mock_gc.get_json = AsyncMock(return_value={"value": []})
        mock_gc_cls.return_value = mock_gc

        resp = client.post(f"/api/excess-lists/{el.id}/solicitations", json={
            "line_item_ids": [items[0].id, items[1].id],
            "recipient_email": "buyer@acme.com",
            "contact_id": 0,
            "bundled": True,
        })
        assert resp.status_code == 201
        # Check subject contains [EXCESS-BID-{first_id}]
        call_args = mock_gc.post_json.call_args
        sent_subject = call_args[0][1]["message"]["subject"]
        assert re.search(r"\[EXCESS-BID-\d+\]", sent_subject)


class TestSendSplit:
    """Split mode: one email per item (existing behavior)."""

    @patch("app.services.excess_service.GraphClient")
    def test_split_sends_one_email_per_item(
        self, mock_gc_cls, client, excess_list_with_items, db_session,
    ):
        el, items = excess_list_with_items
        mock_gc = AsyncMock()
        mock_gc.post_json = AsyncMock(return_value=None)
        mock_gc.get_json = AsyncMock(return_value={"value": []})
        mock_gc_cls.return_value = mock_gc

        resp = client.post(f"/api/excess-lists/{el.id}/solicitations", json={
            "line_item_ids": [i.id for i in items],
            "recipient_email": "buyer@acme.com",
            "contact_id": 0,
            "bundled": False,
        })
        assert resp.status_code == 201
        assert resp.json()["total"] == 3
        # Three separate sendMail calls
        assert mock_gc.post_json.call_count == 3


class TestSendFailure:
    """Graph API errors handled gracefully."""

    @patch("app.services.excess_service.GraphClient")
    def test_graph_failure_marks_solicitation_failed(
        self, mock_gc_cls, client, excess_list_with_items, db_session,
    ):
        el, items = excess_list_with_items
        mock_gc = AsyncMock()
        mock_gc.post_json = AsyncMock(side_effect=Exception("Graph 503"))
        mock_gc_cls.return_value = mock_gc

        resp = client.post(f"/api/excess-lists/{el.id}/solicitations", json={
            "line_item_ids": [items[0].id],
            "recipient_email": "buyer@acme.com",
            "contact_id": 0,
            "bundled": False,
        })
        assert resp.status_code == 201
        assert resp.json()["items"][0]["status"] == "failed"


class TestSendValidation:
    """Input validation."""

    def test_missing_line_items_returns_422(self, client, excess_list_with_items):
        el, _ = excess_list_with_items
        resp = client.post(f"/api/excess-lists/{el.id}/solicitations", json={
            "line_item_ids": [],
            "recipient_email": "buyer@acme.com",
            "contact_id": 0,
        })
        assert resp.status_code == 422
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_solicitations.py -v
```

Expected: FAIL — bundled mode not implemented yet.

- [ ] **Step 3: Commit test file**

```bash
git add tests/test_excess_solicitations.py
git commit -m "test(excess): add solicitation send tests (bundled/split/failure)"
```

---

### Task 3: Implement bundled send + sent-message lookup

**Files:**
- Modify: `app/services/excess_service.py:759-856`

- [ ] **Step 1: Update `send_bid_solicitation()` to support bundled mode with sent-message lookup**

Add `bundled: bool = True` parameter. In bundled mode:
1. Create all BidSolicitation records first (status="pending")
2. Build one HTML email with multi-row parts table using a new `_build_bundled_solicitation_html(items, body_text, recipient_name)` helper
3. Tag subject with first solicitation's ID: `[EXCESS-BID-{first_id}]`
4. Send one email via GraphClient
5. Call `_find_sent_message(gc, email_subject)` (from `email_service.py`) to get `graph_message_id`
6. Set all solicitations to status="sent" with shared `graph_message_id`

In split mode: keep existing per-item loop but add `_find_sent_message()` lookup after each send.

```python
async def send_bid_solicitation(
    db: Session,
    *,
    list_id: int,
    line_item_ids: list[int],
    recipient_email: str,
    recipient_name: str | None,
    contact_id: int,
    user_id: int,
    token: str,
    subject: str | None = None,
    message: str | None = None,
    bundled: bool = True,
) -> list[BidSolicitation]:
```

Add helper function `_build_bundled_solicitation_html(items, body_text, recipient_name)` that generates a multi-row parts table (same styling as `_build_solicitation_html` but with multiple `<tr>` rows).

Import `_find_sent_message` from `email_service` for sent-message lookup (it's a module-private function but shared between RFQ and excess flows — acceptable cross-module import within the same package).

- [ ] **Step 2: Update router to forward `bundled` param**

In `app/routers/excess.py`, update `api_send_solicitations()` (line 570) to pass `bundled`:

```python
    solicitations = await send_bid_solicitation(
        db,
        list_id=list_id,
        line_item_ids=payload.line_item_ids,
        recipient_email=payload.recipient_email,
        recipient_name=payload.recipient_name,
        contact_id=payload.contact_id,
        user_id=user.id,
        token=token,
        subject=payload.subject,
        message=payload.message,
        bundled=payload.bundled,
    )
```

- [ ] **Step 3: Run send tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_solicitations.py -v -k "TestSend"
```

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/services/excess_service.py app/routers/excess.py
git commit -m "feat(excess): bundled solicitation send + sent-message lookup"
```

---

### Task 4: Write and implement AI polish endpoint

**Files:**
- Modify: `app/routers/excess.py`
- Test: `tests/test_excess_solicitations.py`

- [ ] **Step 1: Add polish tests to test file**

```python
class TestPolishEmail:
    """AI email polish endpoint."""

    @patch("app.routers.excess.claude_text", new_callable=AsyncMock)
    def test_polish_returns_cleaned_text(self, mock_claude, client):
        mock_claude.return_value = "Polished version of the email."
        resp = client.post("/api/excess-lists/polish-email", json={
            "text": "hey we got parts u want sum?"
        })
        assert resp.status_code == 200
        assert resp.json()["text"] == "Polished version of the email."
        # Verify prompt includes original text
        call_args = mock_claude.call_args
        assert "hey we got parts u want sum?" in call_args[1].get("prompt", call_args[0][0] if call_args[0] else "")

    def test_polish_empty_text_returns_422(self, client):
        resp = client.post("/api/excess-lists/polish-email", json={"text": ""})
        assert resp.status_code == 422
```

- [ ] **Step 2: Implement endpoint in `app/routers/excess.py`**

Add before the proactive matches section:

```python
@router.post("/api/excess-lists/polish-email")
async def api_polish_email(
    payload: PolishEmailRequest,
    user: User = Depends(require_user),
):
    """Polish a draft email message using AI."""
    from app.utils.claude_client import claude_text

    polished = await claude_text(
        prompt=f"Polish this business email for grammar and professional tone. Keep it concise. Don't change the meaning. Return ONLY the polished text, nothing else.\n\n{payload.text}",
        system="You are a professional email editor. Return only the polished email text.",
        max_tokens=1024,
    )
    return PolishEmailResponse(text=polished.strip())
```

Add imports for `PolishEmailRequest` and `PolishEmailResponse` from `app.schemas.excess`.

- [ ] **Step 3: Run polish tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_solicitations.py -v -k "TestPolish"
```

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/routers/excess.py tests/test_excess_solicitations.py
git commit -m "feat(excess): add AI email polish endpoint"
```

---

### Task 5: Write inbox parsing tests

**Files:**
- Modify: `tests/test_excess_solicitations.py`

- [ ] **Step 1: Add inbox parse tests**

```python
class TestInboxParse:
    """Auto-parsing bid replies from inbox."""

    def _make_solicitation(self, db_session, excess_list_with_items, status="sent"):
        from app.models.excess import BidSolicitation
        el, items = excess_list_with_items
        s = BidSolicitation(
            excess_line_item_id=items[0].id,
            contact_id=0,
            sent_by=1,
            recipient_email="buyer@acme.com",
            recipient_name="John",
            subject=f"Bid Request [EXCESS-BID-PLACEHOLDER]",
            status=status,
            sent_at=datetime.now(timezone.utc),
            graph_message_id="graph-msg-1",
        )
        db_session.add(s)
        db_session.flush()
        s.subject = f"Bid Request [EXCESS-BID-{s.id}]"
        db_session.flush()
        return s

    @patch("app.email_service.claude_structured", new_callable=AsyncMock)
    def test_excess_bid_tag_creates_pending_bid(
        self, mock_claude, db_session, excess_list_with_items,
    ):
        """Message with [EXCESS-BID-{id}] tag creates Bid with status=pending."""
        from app.email_service import _handle_excess_bid_reply
        from app.models.excess import Bid

        s = self._make_solicitation(db_session, excess_list_with_items)
        mock_claude.return_value = {
            "unit_price": 0.38,
            "quantity_wanted": 5000,
            "lead_time_days": 7,
            "notes": "Can ship next week",
        }

        _handle_excess_bid_reply(
            msg={"body": {"content": "We can offer $0.38 each for 5000 pcs"}},
            solicitation_id=s.id,
            db=db_session,
        )
        db_session.flush()

        bid = db_session.query(Bid).filter_by(excess_line_item_id=s.excess_line_item_id).first()
        assert bid is not None
        assert bid.status == "pending"
        assert bid.source == "email_parsed"
        assert float(bid.unit_price) == 0.38
        assert bid.quantity_wanted == 5000

        db_session.refresh(s)
        assert s.status == "responded"
        assert s.parsed_bid_id == bid.id

    @patch("app.email_service.claude_structured", new_callable=AsyncMock)
    def test_declined_response_no_bid_created(
        self, mock_claude, db_session, excess_list_with_items,
    ):
        """Declined response marks solicitation responded but creates no Bid."""
        from app.email_service import _handle_excess_bid_reply
        from app.models.excess import Bid

        s = self._make_solicitation(db_session, excess_list_with_items)
        mock_claude.return_value = {"declined": True}

        _handle_excess_bid_reply(
            msg={"body": {"content": "Sorry, not interested at this time."}},
            solicitation_id=s.id,
            db=db_session,
        )
        db_session.flush()

        bid = db_session.query(Bid).filter_by(excess_line_item_id=s.excess_line_item_id).first()
        assert bid is None
        db_session.refresh(s)
        assert s.status == "responded"

    def test_already_responded_skipped(self, db_session, excess_list_with_items):
        """Solicitation already responded is skipped."""
        from app.email_service import _handle_excess_bid_reply

        s = self._make_solicitation(db_session, excess_list_with_items, status="responded")
        # Should return early without error
        _handle_excess_bid_reply(
            msg={"body": {"content": "duplicate"}},
            solicitation_id=s.id,
            db=db_session,
        )

    def test_solicitation_not_found_skipped(self, db_session):
        """Bad solicitation ID logged and skipped."""
        from app.email_service import _handle_excess_bid_reply

        # Should not raise
        _handle_excess_bid_reply(
            msg={"body": {"content": "test"}},
            solicitation_id=99999,
            db=db_session,
        )

    def test_lookback_window_skips_old_solicitations(self, db_session, excess_list_with_items):
        """Solicitations older than lookback window are skipped."""
        from app.email_service import _handle_excess_bid_reply
        from app.models.excess import BidSolicitation

        s = self._make_solicitation(db_session, excess_list_with_items)
        # Set sent_at to 30 days ago (beyond 14-day default)
        s.sent_at = datetime.now(timezone.utc) - timedelta(days=30)
        db_session.flush()

        # Should skip without calling Claude
        _handle_excess_bid_reply(
            msg={"body": {"content": "We offer $0.50 each"}},
            solicitation_id=s.id,
            db=db_session,
        )
        db_session.refresh(s)
        assert s.status == "sent"  # unchanged — skipped due to lookback

    @patch("app.email_service.claude_structured", new_callable=AsyncMock)
    def test_parse_failure_leaves_solicitation_sent(
        self, mock_claude, db_session, excess_list_with_items,
    ):
        """Parse failure leaves solicitation as sent."""
        from app.email_service import _handle_excess_bid_reply

        s = self._make_solicitation(db_session, excess_list_with_items)
        mock_claude.side_effect = Exception("Claude API error")

        _handle_excess_bid_reply(
            msg={"body": {"content": "garbled text"}},
            solicitation_id=s.id,
            db=db_session,
        )
        db_session.refresh(s)
        assert s.status == "sent"  # unchanged
```

- [ ] **Step 2: Run to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_solicitations.py -v -k "TestInbox"
```

Expected: FAIL — `_handle_excess_bid_reply` doesn't exist yet.

- [ ] **Step 3: Commit tests**

```bash
git add tests/test_excess_solicitations.py
git commit -m "test(excess): add inbox parse tests for bid solicitation replies"
```

---

### Task 6: Implement `_handle_excess_bid_reply()` and hook into `poll_inbox()`

**Files:**
- Modify: `app/email_service.py`

- [ ] **Step 1: Add regex constant and handler function**

Near the top of `email_service.py` (after imports), add:

```python
_EXCESS_BID_RE = re.compile(r"\[EXCESS-BID-(\d+)\]")
```

Add the handler function (place near `_auto_create_offers_from_parse`):

```python
def _handle_excess_bid_reply(msg: dict, solicitation_id: int, db: Session) -> None:
    """Parse an inbox reply to an excess bid solicitation and create a pending Bid.

    Called by: poll_inbox() when [EXCESS-BID-{id}] tag detected in subject.
    Depends on: claude_structured, parse_bid_response (excess_service).
    """
    from .models.excess import BidSolicitation
    from .services.excess_service import parse_bid_response

    solicitation = db.get(BidSolicitation, solicitation_id)
    if not solicitation:
        logger.warning("Excess bid solicitation {} not found, skipping", solicitation_id)
        return

    if solicitation.status in ("responded", "expired"):
        logger.debug("Solicitation {} already {}, skipping", solicitation_id, solicitation.status)
        return

    # Lookback window — ignore solicitations older than configured days
    from .config import settings
    if solicitation.sent_at:
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.excess_bid_parse_lookback_days)
        if solicitation.sent_at < cutoff:
            logger.debug("Solicitation {} sent_at {} before lookback cutoff, skipping", solicitation_id, solicitation.sent_at)
            return

    body = msg.get("body", {}).get("content", msg.get("bodyPreview", ""))
    if not body.strip():
        logger.debug("Empty body for solicitation {} reply, skipping", solicitation_id)
        return

    # Parse with Claude
    try:
        import asyncio
        from .utils.claude_client import claude_structured

        item = solicitation.excess_line_item
        prompt = (
            f"Extract bid info from this email reply to a parts solicitation.\n"
            f"Original request: {item.part_number} x {item.quantity}, "
            f"asking ${item.asking_price or '?'}.\n\n"
            f"Email body:\n{body[:2000]}\n\n"
            f"Return JSON: {{\"unit_price\": float|null, \"quantity_wanted\": int|null, "
            f"\"lead_time_days\": int|null, \"notes\": str|null}}\n"
            f"If the email declines to bid, return {{\"declined\": true}}."
        )

        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = loop.run_in_executor(pool, lambda: asyncio.run(
                    claude_structured(prompt=prompt, schema={
                        "type": "object",
                        "properties": {
                            "unit_price": {"type": ["number", "null"]},
                            "quantity_wanted": {"type": ["integer", "null"]},
                            "lead_time_days": {"type": ["integer", "null"]},
                            "notes": {"type": ["string", "null"]},
                            "declined": {"type": "boolean"},
                        },
                    }, max_tokens=512)
                ))
        else:
            result = asyncio.run(claude_structured(prompt=prompt, schema={
                "type": "object",
                "properties": {
                    "unit_price": {"type": ["number", "null"]},
                    "quantity_wanted": {"type": ["integer", "null"]},
                    "lead_time_days": {"type": ["integer", "null"]},
                    "notes": {"type": ["string", "null"]},
                    "declined": {"type": "boolean"},
                },
            }, max_tokens=512))
    except Exception as e:
        logger.warning("Failed to parse excess bid reply for solicitation {}: {}", solicitation_id, e)
        return  # Leave status as "sent"

    if not result:
        logger.warning("Empty parse result for solicitation {}", solicitation_id)
        return

    # Handle decline
    if result.get("declined"):
        solicitation.status = "responded"
        solicitation.response_received_at = datetime.now(timezone.utc)
        logger.info("Solicitation {} declined by recipient", solicitation_id)
        return

    # Extract fields and create Bid
    unit_price = result.get("unit_price")
    qty = result.get("quantity_wanted")
    if not unit_price or not qty:
        logger.warning("Incomplete parse for solicitation {}: {}", solicitation_id, result)
        return  # Leave as "sent" for manual entry

    bid = parse_bid_response(
        db,
        solicitation_id=solicitation_id,
        unit_price=unit_price,
        quantity_wanted=qty,
        lead_time_days=result.get("lead_time_days"),
        notes=result.get("notes"),
    )

    # Create ActivityLog notification
    from .models import ActivityLog
    item = solicitation.excess_line_item
    db.add(ActivityLog(
        user_id=solicitation.sent_by,
        activity_type="bid_received",
        channel="system",
        subject=f"New bid received (pending review): {solicitation.recipient_name or solicitation.recipient_email} — {item.part_number}",
    ))

    logger.info("Auto-created bid {} from solicitation {} reply", bid.id, solicitation_id)
```

**Note:** The spec mentions `conversationId` matching for replies that strip subject tags. The `BidSolicitation` model has no `conversationId` column, so we rely solely on subject tag `[EXCESS-BID-{id}]` matching. If this proves insufficient in practice, a migration can add a `graph_conversation_id` column later.

- [ ] **Step 2: Hook into `poll_inbox()`**

In `poll_inbox()`, after the existing RFQ subject tag matching block, add excess bid tag detection. Find where messages are iterated and tags checked — add:

```python
# Check for excess bid solicitation replies
excess_bid_match = _EXCESS_BID_RE.search(subject)
if excess_bid_match:
    sol_id = int(excess_bid_match.group(1))
    _handle_excess_bid_reply(msg, sol_id, db)
    continue  # Don't also process as RFQ
```

- [ ] **Step 3: Run inbox parse tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_solicitations.py -v -k "TestInbox"
```

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/email_service.py
git commit -m "feat(excess): add inbox bid reply parsing + hook into poll_inbox"
```

---

### Task 7: Write and implement integration test (full round-trip)

**Files:**
- Modify: `tests/test_excess_solicitations.py`

- [ ] **Step 1: Add integration test**

```python
class TestRoundTrip:
    """Full send → receive → parse → bid flow."""

    @patch("app.email_service.claude_structured", new_callable=AsyncMock)
    @patch("app.services.excess_service.GraphClient")
    def test_send_then_parse_creates_bid(
        self, mock_gc_cls, mock_claude, client, excess_list_with_items, db_session,
    ):
        from app.email_service import _handle_excess_bid_reply
        from app.models.excess import Bid, BidSolicitation

        el, items = excess_list_with_items

        # Step 1: Send solicitation
        mock_gc = AsyncMock()
        mock_gc.post_json = AsyncMock(return_value=None)
        mock_gc.get_json = AsyncMock(return_value={
            "value": [{"id": "msg-123", "conversationId": "conv-1", "subject": "test"}]
        })
        mock_gc_cls.return_value = mock_gc

        resp = client.post(f"/api/excess-lists/{el.id}/solicitations", json={
            "line_item_ids": [items[0].id],
            "recipient_email": "buyer@acme.com",
            "contact_id": 0,
            "bundled": False,
        })
        assert resp.status_code == 201
        sol_id = resp.json()["items"][0]["id"]

        # Step 2: Simulate reply parsing
        mock_claude.return_value = {
            "unit_price": 0.40,
            "quantity_wanted": 5000,
            "lead_time_days": 5,
            "notes": "FOB Shanghai",
        }
        _handle_excess_bid_reply(
            msg={"body": {"content": "We offer $0.40 per unit for 5000 pcs, 5 day lead time."}},
            solicitation_id=sol_id,
            db=db_session,
        )
        db_session.flush()

        # Step 3: Verify bid created
        bid = db_session.query(Bid).filter_by(excess_line_item_id=items[0].id).first()
        assert bid is not None
        assert bid.source == "email_parsed"
        assert float(bid.unit_price) == 0.40

        # Step 4: Verify solicitation updated
        sol = db_session.get(BidSolicitation, sol_id)
        assert sol.status == "responded"
        assert sol.parsed_bid_id == bid.id

        # Step 5: Verify ActivityLog notification
        from app.models import ActivityLog
        notif = db_session.query(ActivityLog).filter(
            ActivityLog.activity_type == "bid_received",
        ).first()
        assert notif is not None
        assert items[0].part_number in notif.subject
```

- [ ] **Step 2: Run full test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_solicitations.py -v
```

Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_excess_solicitations.py
git commit -m "test(excess): add full round-trip integration test"
```

---

### Task 8: Create solicitation modal template

**Files:**
- Create: `app/templates/htmx/partials/excess/solicit_modal.html`
- Modify: `app/templates/htmx/partials/excess/detail.html`
- Modify: `app/routers/excess.py` (add partial route for modal)

- [ ] **Step 1: Create solicit_modal.html**

HTMX modal with: recipient search field (Alpine.js for dropdown), subject input, bundled checkbox, message textarea, AI Clean Up button, read-only parts table, Cancel + Send buttons.

Key interactions:
- Recipient field: `hx-get="/api/contacts/search?q=..."` with `hx-trigger="keyup changed delay:300ms"` for contact search dropdown
- Clean Up button: `hx-post="/api/excess-lists/polish-email"` targeting the textarea, `hx-swap="innerHTML"` on a hidden div that Alpine reads back into textarea
- Send button: `hx-post="/api/excess-lists/{list_id}/solicitations"` with `hx-include` for form fields
- Parts table: rendered server-side from selected line items

Use Alpine.js `x-data` for form state (selectedItems, recipientEmail, recipientName, contactId, bundled, message).

Follow frontend-design skill aesthetic: industrial/utilitarian, matching existing AvailAI modal style from `create_modal.html` and `bid_form.html`.

- [ ] **Step 2: Add "Solicit Bids" button to detail.html**

Add a button in the detail page toolbar (near existing action buttons) that opens the modal:
```html
<button hx-get="/v2/partials/excess/{{ list.id }}/solicit-modal?item_ids=..."
        hx-target="#modal-container" hx-swap="innerHTML"
        class="btn btn-primary">
    Solicit Bids
</button>
```

- [ ] **Step 3: Add partial route for modal**

In `app/routers/excess.py`:

```python
@router.get("/v2/partials/excess/{list_id}/solicit-modal")
async def partial_solicit_modal(
    list_id: int,
    item_ids: str = Query(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the solicitation modal with selected items."""
    ids = [int(x) for x in item_ids.split(",") if x.strip().isdigit()]
    items = db.query(ExcessLineItem).filter(
        ExcessLineItem.id.in_(ids),
        ExcessLineItem.excess_list_id == list_id,
    ).all() if ids else []
    excess_list = get_excess_list(db, list_id)
    return templates.TemplateResponse("htmx/partials/excess/solicit_modal.html", {
        "request": request, "list": excess_list, "items": items, "user": user,
    })
```

- [ ] **Step 4: Commit**

```bash
git add app/templates/htmx/partials/excess/solicit_modal.html app/templates/htmx/partials/excess/detail.html app/routers/excess.py
git commit -m "feat(excess): add solicitation modal template + detail button"
```

---

### Task 9: Run full test suite and deploy

**Files:** None (verification only)

- [ ] **Step 1: Run full test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v
```

Expected: All existing + new tests PASS

- [ ] **Step 2: Run targeted excess tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_solicitations.py tests/test_excess*.py -v
```

- [ ] **Step 3: Commit any remaining changes, push, rebuild**

```bash
git push origin main && docker compose up -d --build
```

- [ ] **Step 4: Check logs**

```bash
docker compose logs --tail=20 app
```

Expected: Uvicorn running, no import errors.
