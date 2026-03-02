# AVAIL Self-Heal Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a 5-phase AI-managed trouble ticket and self-repair pipeline where users submit bug reports, the system auto-captures context, diagnoses via Claude AI, generates constrained repair prompts, and executes fixes locally via subprocess (pluggable backend for future GitHub Actions support).

**Architecture:** SPA views in `tickets.js` (matching existing pattern), stateless service functions (matching codebase), existing `claude_json`/`claude_structured` API integration, migrations 039-041.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, PostgreSQL, Jinja2, vanilla JS, Claude API (via `app/utils/claude_client.py`), local subprocess execution.

**Security Note:** The `tickets.js` UI uses innerHTML for rendering. All ticket data originates from authenticated users and is displayed to the same user or admins. User-provided text (title, description) must be HTML-escaped before rendering via innerHTML. Use a helper like `function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}` and apply it to all user-provided fields.

**Codebase Patterns Reference:**
- Model base: `from app.models.base import Base` (see `app/models/sourcing.py`)
- Auth deps: `from app.dependencies import require_user, require_admin` (see `app/dependencies.py`)
- DB session: `db: Session = Depends(get_db)` from `app.database`
- Config: `Settings` class in `app/config.py`, env vars via `os.getenv()`
- Tests: `db_session` fixture (auto-rollback), `client` fixture (TestClient with auth overrides), `admin_user` fixture
- Error responses: `{"error": str, "status_code": int, "request_id": str}`
- Pagination: `{items, total, limit, offset}`
- Router registration: `from .routers.X import router as X_router` then `app.include_router(X_router)` in `app/main.py`
- Model registration: add imports to `app/models/__init__.py`
- Migration pattern: sequential `039_name.py`, `revision = "039_name"`, `down_revision = "038_api_health_monitoring"`
- Logging: Loguru, never print()

---

## PHASE 1: Ticket Model + Submission UI + Auto-Context

---

### Task 1: TroubleTicket Model

**Files:**
- Create: `app/models/trouble_ticket.py`
- Modify: `app/models/__init__.py` (add import)

**Step 1: Create the model file**

Create `app/models/trouble_ticket.py`:

```python
"""Trouble ticket model — user-submitted bug reports for the self-heal pipeline.

Tracks the full lifecycle: submitted -> diagnosed -> fix_proposed -> fix_in_progress ->
fix_applied -> awaiting_verification -> resolved (or escalated/fix_reverted).

Called by: routers/trouble_tickets.py, services/trouble_ticket_service.py
Depends on: models/base.py, models/auth.py (User FK)
"""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .base import Base


class TroubleTicket(Base):
    __tablename__ = "trouble_tickets"
    __table_args__ = (
        Index("ix_trouble_tickets_status", "status"),
        Index("ix_trouble_tickets_risk_tier", "risk_tier"),
        Index("ix_trouble_tickets_submitted_by", "submitted_by"),
        Index("ix_trouble_tickets_created_at", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    ticket_number = Column(String(20), unique=True, nullable=False)
    submitted_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    status = Column(String(30), default="submitted", nullable=False)
    risk_tier = Column(String(10))
    category = Column(String(20))
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=False)
    current_page = Column(String(500))
    user_agent = Column(String(500))
    auto_captured_context = Column(JSON)
    sanitized_context = Column(JSON)
    diagnosis = Column(JSON)
    generated_prompt = Column(Text)
    file_mapping = Column(JSON)
    fix_branch = Column(String(200))
    fix_pr_url = Column(String(500))
    iterations_used = Column(Integer)
    cost_tokens = Column(Integer)
    cost_usd = Column(Float)
    resolution_notes = Column(Text)
    parent_ticket_id = Column(Integer, ForeignKey("trouble_tickets.id", ondelete="SET NULL"))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, onupdate=lambda: datetime.now(timezone.utc))
    diagnosed_at = Column(DateTime)
    resolved_at = Column(DateTime)

    submitter = relationship("User", foreign_keys=[submitted_by])
    parent_ticket = relationship("TroubleTicket", remote_side=[id], foreign_keys=[parent_ticket_id])
```

**Step 2: Register in models/__init__.py**

Add after the `# Error Reports / Trouble Tickets` section (around line 37) in `app/models/__init__.py`:

```python
from .trouble_ticket import TroubleTicket  # noqa: F401
```

**Step 3: Verify import works**

Run: `cd /root/availai && TESTING=1 python -c "from app.models import TroubleTicket; print('OK:', TroubleTicket.__tablename__)"`
Expected: `OK: trouble_tickets`

**Step 4: Commit**

```bash
git add app/models/trouble_ticket.py app/models/__init__.py
git commit -m "feat: add TroubleTicket model for self-heal pipeline"
```

---

### Task 2: Pydantic Schemas

**Files:**
- Create: `app/schemas/trouble_ticket.py`

**Step 1: Create the schema file**

Create `app/schemas/trouble_ticket.py`:

```python
"""Pydantic schemas for trouble ticket request/response validation.

Called by: routers/trouble_tickets.py
Depends on: nothing (pure validation)
"""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class TroubleTicketCreate(BaseModel):
    title: str = Field(..., max_length=200)
    description: str
    current_page: str | None = None
    frontend_errors: list[dict] | None = None

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Title is required")
        return v

    @field_validator("description")
    @classmethod
    def description_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Description is required")
        return v


class TroubleTicketUpdate(BaseModel):
    status: str | None = None
    resolution_notes: str | None = None
    risk_tier: str | None = None
    category: str | None = None


class TroubleTicketResponse(BaseModel, extra="allow"):
    id: int
    ticket_number: str
    submitted_by: int | None = None
    submitted_by_name: str | None = None
    status: str
    risk_tier: str | None = None
    category: str | None = None
    title: str
    description: str
    current_page: str | None = None
    auto_captured_context: dict | None = None
    sanitized_context: dict | None = None
    diagnosis: dict | None = None
    generated_prompt: str | None = None
    file_mapping: list | None = None
    fix_branch: str | None = None
    fix_pr_url: str | None = None
    iterations_used: int | None = None
    cost_tokens: int | None = None
    cost_usd: float | None = None
    resolution_notes: str | None = None
    parent_ticket_id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    diagnosed_at: datetime | None = None
    resolved_at: datetime | None = None
```

**Step 2: Verify import**

Run: `cd /root/availai && TESTING=1 python -c "from app.schemas.trouble_ticket import TroubleTicketCreate; print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add app/schemas/trouble_ticket.py
git commit -m "feat: add trouble ticket Pydantic schemas"
```

---

### Task 3: Service Layer + Tests

**Files:**
- Create: `app/services/trouble_ticket_service.py`
- Create: `tests/test_trouble_tickets.py`

**Step 1: Write the tests first**

Create `tests/test_trouble_tickets.py`:

```python
"""Tests for trouble ticket service and router.

Covers: ticket creation, auto-context capture, sanitization,
ticket number generation, listing, access control, verify endpoint.

Called by: pytest
Depends on: conftest fixtures, app.models.TroubleTicket
"""

import re
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import User
from app.models.trouble_ticket import TroubleTicket
from app.services.trouble_ticket_service import (
    create_ticket,
    get_ticket,
    list_tickets,
    get_tickets_by_user,
    check_file_lock,
    _capture_auto_context,
    _sanitize_context,
)


class TestTicketCreation:
    def test_create_ticket_basic(self, db_session: Session, test_user: User):
        ticket = create_ticket(
            db=db_session,
            user_id=test_user.id,
            title="Button not working",
            description="The submit button on the RFQ page does nothing when clicked.",
            current_page="/api/rfq",
            user_agent="Mozilla/5.0",
            frontend_errors=[],
        )
        assert ticket.id is not None
        assert ticket.status == "submitted"
        assert ticket.submitted_by == test_user.id
        assert ticket.title == "Button not working"

    def test_ticket_number_format(self, db_session: Session, test_user: User):
        ticket = create_ticket(
            db=db_session,
            user_id=test_user.id,
            title="Test",
            description="Test description",
        )
        assert re.match(r"TT-\d{8}-\d{3,}", ticket.ticket_number)

    def test_ticket_number_sequential(self, db_session: Session, test_user: User):
        t1 = create_ticket(db=db_session, user_id=test_user.id, title="First", description="First ticket")
        t2 = create_ticket(db=db_session, user_id=test_user.id, title="Second", description="Second ticket")
        num1 = int(t1.ticket_number.rsplit("-", 1)[1])
        num2 = int(t2.ticket_number.rsplit("-", 1)[1])
        assert num2 == num1 + 1


class TestAutoContext:
    def test_capture_returns_expected_structure(self, db_session: Session, test_user: User):
        ctx = _capture_auto_context(db=db_session, user_id=test_user.id, current_page="/api/vendors/123")
        assert "recent_api_errors" in ctx
        assert "recent_frontend_errors" in ctx
        assert "user_role" in ctx
        assert "server_info" in ctx
        assert "page_route" in ctx
        assert ctx["user_role"] == "buyer"

    def test_page_route_parameterized(self, db_session: Session, test_user: User):
        ctx = _capture_auto_context(db=db_session, user_id=test_user.id, current_page="/api/vendors/12345")
        assert ctx["page_route"] == "/api/vendors/{id}"


class TestSanitization:
    def test_strips_api_keys(self):
        ctx = {"error": "Failed with key sk-ant-api03-abc123xyz"}
        result = _sanitize_context(ctx, submitter_email="test@example.com")
        assert "sk-ant-api03-abc123xyz" not in str(result)

    def test_strips_bearer_tokens(self):
        ctx = {"header": "Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.abc.def"}
        result = _sanitize_context(ctx, submitter_email="test@example.com")
        assert "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9" not in str(result)

    def test_strips_connection_strings(self):
        ctx = {"db": "postgres://user:pass@host:5432/db"}
        result = _sanitize_context(ctx, submitter_email="test@example.com")
        assert "postgres://user:pass" not in str(result)

    def test_strips_passwords(self):
        ctx = {"config": 'password = "hunter2"'}
        result = _sanitize_context(ctx, submitter_email="test@example.com")
        assert "hunter2" not in str(result)

    def test_strips_api_key_values(self):
        ctx = {"config": 'api_key = "my-secret-key-123"'}
        result = _sanitize_context(ctx, submitter_email="test@example.com")
        assert "my-secret-key-123" not in str(result)

    def test_replaces_emails_except_submitter(self):
        ctx = {"data": "Contact admin@company.com and other@example.com"}
        result = _sanitize_context(ctx, submitter_email="admin@company.com")
        result_str = str(result)
        assert "admin@company.com" in result_str
        assert "other@example.com" not in result_str
        assert "[EMAIL]" in result_str

    def test_replaces_ip_addresses(self):
        ctx = {"server": "Connected to 192.168.1.100"}
        result = _sanitize_context(ctx, submitter_email="test@example.com")
        assert "192.168.1.100" not in str(result)
        assert "[IP]" in str(result)

    def test_truncates_long_messages(self):
        ctx = {"error": "x" * 1000}
        result = _sanitize_context(ctx, submitter_email="test@example.com")
        assert len(result["error"]) <= 500


class TestTicketQueries:
    def test_get_ticket(self, db_session: Session, test_user: User):
        ticket = create_ticket(db=db_session, user_id=test_user.id, title="Test", description="Desc")
        found = get_ticket(db=db_session, ticket_id=ticket.id)
        assert found is not None
        assert found.id == ticket.id

    def test_get_ticket_not_found(self, db_session: Session):
        found = get_ticket(db=db_session, ticket_id=99999)
        assert found is None

    def test_list_tickets_with_status_filter(self, db_session: Session, test_user: User):
        create_ticket(db=db_session, user_id=test_user.id, title="T1", description="D1")
        t2 = create_ticket(db=db_session, user_id=test_user.id, title="T2", description="D2")
        t2.status = "diagnosed"
        db_session.commit()
        result = list_tickets(db=db_session, status_filter="submitted")
        assert result["total"] == 1
        assert result["items"][0]["status"] == "submitted"

    def test_list_tickets_pagination(self, db_session: Session, test_user: User):
        for i in range(5):
            create_ticket(db=db_session, user_id=test_user.id, title=f"T{i}", description=f"D{i}")
        result = list_tickets(db=db_session, limit=2, offset=0)
        assert result["total"] == 5
        assert len(result["items"]) == 2

    def test_get_tickets_by_user(self, db_session: Session, test_user: User, admin_user: User):
        create_ticket(db=db_session, user_id=test_user.id, title="User ticket", description="D")
        create_ticket(db=db_session, user_id=admin_user.id, title="Admin ticket", description="D")
        user_tickets = get_tickets_by_user(db=db_session, user_id=test_user.id)
        assert len(user_tickets) == 1
        assert user_tickets[0].title == "User ticket"


class TestFileLock:
    def test_no_conflict(self, db_session: Session, test_user: User):
        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        ticket.status = "fix_in_progress"
        ticket.file_mapping = ["app/routers/vendors.py"]
        db_session.commit()
        result = check_file_lock(db=db_session, file_paths=["app/routers/crm.py"])
        assert result is None

    def test_conflict_detected(self, db_session: Session, test_user: User):
        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        ticket.status = "fix_in_progress"
        ticket.file_mapping = ["app/routers/vendors.py", "app/services/vendor_service.py"]
        db_session.commit()
        result = check_file_lock(db=db_session, file_paths=["app/routers/vendors.py"])
        assert result is not None
        assert result.id == ticket.id


class TestVerifyEndpoint:
    def test_verify_resolved(self, client, db_session: Session, test_user: User):
        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        ticket.status = "awaiting_verification"
        db_session.commit()
        resp = client.post(f"/api/trouble-tickets/{ticket.id}/verify", json={"is_fixed": True})
        assert resp.status_code == 200
        db_session.refresh(ticket)
        assert ticket.status == "resolved"

    def test_verify_still_broken_creates_child(self, client, db_session: Session, test_user: User):
        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        ticket.status = "awaiting_verification"
        ticket.risk_tier = "low"
        db_session.commit()
        resp = client.post(
            f"/api/trouble-tickets/{ticket.id}/verify",
            json={"is_fixed": False, "description": "Still broken"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "child_ticket_id" in data
        child = db_session.get(TroubleTicket, data["child_ticket_id"])
        assert child.parent_ticket_id == ticket.id
        assert child.risk_tier in ("medium", "high")


class TestRouterEndpoints:
    def test_create_ticket_endpoint(self, client, db_session: Session):
        resp = client.post("/api/trouble-tickets", json={
            "title": "Test ticket",
            "description": "Something broke",
            "current_page": "/vendors",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "ticket_number" in data

    def test_my_tickets_endpoint(self, client, db_session: Session, test_user: User):
        create_ticket(db=db_session, user_id=test_user.id, title="My ticket", description="D")
        resp = client.get("/api/trouble-tickets/my-tickets")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 1
```

**Step 2: Run tests to verify they fail**

Run: `cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_trouble_tickets.py -v --tb=short 2>&1 | head -20`
Expected: ImportError for `trouble_ticket_service`

**Step 3: Write the service**

Create `app/services/trouble_ticket_service.py`:

```python
"""Trouble ticket service -- CRUD, auto-context capture, and sanitization.

Handles the full ticket lifecycle: creation with auto-context capture,
sanitization of sensitive data before AI processing, listing, updating,
and file lock checking for concurrent fix prevention.

Called by: routers/trouble_tickets.py
Depends on: models/trouble_ticket.py, models/auth.py, config.py
"""

import re
import sys
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.config import APP_VERSION
from app.models.trouble_ticket import TroubleTicket
from app.models import User


_SENSITIVE_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9_-]{20,}"),
    re.compile(r"Bearer\s+\S+"),
    re.compile(r"postgres(ql)?://\S+"),
    re.compile(r'password["\s:=]+\S+', re.IGNORECASE),
    re.compile(r'api[_-]?key["\s:=]+\S+', re.IGNORECASE),
    re.compile(r'secret["\s:=]+\S+', re.IGNORECASE),
]
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_IP_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
_NUMERIC_SEGMENT_RE = re.compile(r"/\d+")


def create_ticket(
    db: Session,
    user_id: int,
    title: str,
    description: str,
    current_page: str | None = None,
    user_agent: str | None = None,
    frontend_errors: list[dict] | None = None,
) -> TroubleTicket:
    """Create a trouble ticket with auto-captured context."""
    ticket_number = _generate_ticket_number(db)

    auto_ctx = _capture_auto_context(
        db=db,
        user_id=user_id,
        current_page=current_page,
        frontend_errors=frontend_errors,
    )

    user = db.get(User, user_id)
    submitter_email = user.email if user else ""
    sanitized = _sanitize_context(auto_ctx, submitter_email=submitter_email)

    ticket = TroubleTicket(
        ticket_number=ticket_number,
        submitted_by=user_id,
        title=title.strip(),
        description=description.strip(),
        current_page=current_page,
        user_agent=user_agent,
        auto_captured_context=auto_ctx,
        sanitized_context=sanitized,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)

    logger.info(
        "Ticket {ticket_number} created by user {user_id}",
        ticket_number=ticket_number,
        user_id=user_id,
    )
    return ticket


def get_ticket(db: Session, ticket_id: int) -> TroubleTicket | None:
    """Get a single ticket by ID."""
    return db.get(TroubleTicket, ticket_id)


def list_tickets(
    db: Session,
    status_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Paginated list of tickets, optionally filtered by status."""
    query = db.query(TroubleTicket)
    if status_filter:
        query = query.filter(TroubleTicket.status == status_filter)
    total = query.count()
    tickets = (
        query.order_by(TroubleTicket.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    items = []
    for t in tickets:
        submitter = db.get(User, t.submitted_by) if t.submitted_by else None
        items.append({
            "id": t.id,
            "ticket_number": t.ticket_number,
            "title": t.title,
            "status": t.status,
            "risk_tier": t.risk_tier,
            "category": t.category,
            "submitted_by": t.submitted_by,
            "submitted_by_name": submitter.name if submitter else None,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        })
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def get_tickets_by_user(db: Session, user_id: int) -> list[TroubleTicket]:
    """List tickets submitted by a specific user."""
    return (
        db.query(TroubleTicket)
        .filter(TroubleTicket.submitted_by == user_id)
        .order_by(TroubleTicket.created_at.desc())
        .all()
    )


def update_ticket(db: Session, ticket_id: int, **kwargs) -> TroubleTicket | None:
    """Update ticket fields. Auto-sets diagnosed_at/resolved_at on transitions."""
    ticket = db.get(TroubleTicket, ticket_id)
    if not ticket:
        return None

    for key, value in kwargs.items():
        if hasattr(ticket, key):
            setattr(ticket, key, value)

    if "status" in kwargs:
        if kwargs["status"] == "diagnosed" and not ticket.diagnosed_at:
            ticket.diagnosed_at = datetime.now(timezone.utc)
        elif kwargs["status"] == "resolved" and not ticket.resolved_at:
            ticket.resolved_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(ticket)
    return ticket


def check_file_lock(db: Session, file_paths: list[str]) -> TroubleTicket | None:
    """Check if any active fix overlaps with the given files."""
    active = (
        db.query(TroubleTicket)
        .filter(TroubleTicket.status == "fix_in_progress")
        .filter(TroubleTicket.file_mapping.isnot(None))
        .all()
    )
    file_set = set(file_paths)
    for ticket in active:
        if ticket.file_mapping and set(ticket.file_mapping) & file_set:
            return ticket
    return None


def _generate_ticket_number(db: Session) -> str:
    """Generate TT-YYYYMMDD-NNN ticket number."""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    prefix = f"TT-{today}-"
    last = (
        db.query(TroubleTicket)
        .filter(TroubleTicket.ticket_number.like(f"{prefix}%"))
        .order_by(TroubleTicket.id.desc())
        .first()
    )
    if last:
        last_num = int(last.ticket_number.split("-")[-1])
        seq = last_num + 1
    else:
        seq = 1
    return f"{prefix}{seq:03d}"


def _capture_auto_context(
    db: Session,
    user_id: int,
    current_page: str | None = None,
    frontend_errors: list[dict] | None = None,
) -> dict:
    """Build auto-captured context dict from user session and server state."""
    user = db.get(User, user_id)
    user_role = user.role if user else "unknown"

    page_route = current_page or ""
    if page_route:
        page_route = _NUMERIC_SEGMENT_RE.sub("/{id}", page_route)

    return {
        "recent_api_errors": [],  # TODO: integrate with Sentry API
        "recent_frontend_errors": frontend_errors or [],
        "user_role": user_role,
        "server_info": {
            "python_version": sys.version.split()[0],
            "app_version": APP_VERSION,
        },
        "page_route": page_route,
    }


def _sanitize_context(context: dict, submitter_email: str = "") -> dict:
    """Strip sensitive data from context before AI processing."""
    sanitized = {}
    any_stripped = False

    for key, value in context.items():
        if isinstance(value, str):
            clean = _sanitize_string(value, submitter_email)
            sanitized[key] = clean
            if clean != value:
                any_stripped = True
        elif isinstance(value, dict):
            sanitized[key] = _sanitize_context(value, submitter_email)
        elif isinstance(value, list):
            sanitized[key] = [
                _sanitize_context(item, submitter_email) if isinstance(item, dict)
                else _sanitize_string(item, submitter_email) if isinstance(item, str)
                else item
                for item in value
            ]
        else:
            sanitized[key] = value

    if any_stripped:
        logger.warning("Sensitive data stripped from ticket context")

    return sanitized


def _sanitize_string(value: str, submitter_email: str) -> str:
    """Sanitize a single string value."""
    result = value
    for pattern in _SENSITIVE_PATTERNS:
        result = pattern.sub("[REDACTED]", result)

    def _replace_email(match):
        email = match.group(0)
        if email.lower() == submitter_email.lower():
            return email
        return "[EMAIL]"

    result = _EMAIL_RE.sub(_replace_email, result)
    result = _IP_RE.sub("[IP]", result)

    if len(result) > 500:
        result = result[:500]

    return result
```

**Step 4: Run tests**

Run: `cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_trouble_tickets.py -v --tb=short 2>&1 | tail -30`
Expected: Service tests pass, router tests fail (router not yet created)

**Step 5: Commit**

```bash
git add app/services/trouble_ticket_service.py tests/test_trouble_tickets.py
git commit -m "feat: add trouble ticket service with auto-context and sanitization"
```

---

### Task 4: Router

**Files:**
- Create: `app/routers/trouble_tickets.py`
- Modify: `app/main.py` (add router include)

**Step 1: Create the router**

Create `app/routers/trouble_tickets.py`:

```python
"""Trouble ticket router -- CRUD endpoints for the self-heal pipeline.

POST /api/trouble-tickets            -- create (any authenticated user)
GET  /api/trouble-tickets            -- list all (admin, status filter + pagination)
GET  /api/trouble-tickets/my-tickets -- current user's tickets
GET  /api/trouble-tickets/{id}       -- single ticket (admin or submitter)
PATCH /api/trouble-tickets/{id}      -- update (admin only)
POST /api/trouble-tickets/{id}/verify -- user confirms fix or reports still broken

Called by: main.py (app.include_router)
Depends on: services/trouble_ticket_service.py, dependencies.py
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_admin, require_user
from app.models import User
from app.schemas.trouble_ticket import TroubleTicketCreate, TroubleTicketUpdate
from app.services import trouble_ticket_service as svc

router = APIRouter(tags=["trouble-tickets"])


@router.post("/api/trouble-tickets")
async def create_ticket(
    body: TroubleTicketCreate,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Submit a new trouble ticket. Any authenticated user."""
    ticket = svc.create_ticket(
        db=db,
        user_id=user.id,
        title=body.title,
        description=body.description,
        current_page=body.current_page,
        user_agent=request.headers.get("user-agent"),
        frontend_errors=body.frontend_errors,
    )
    return {"ok": True, "id": ticket.id, "ticket_number": ticket.ticket_number}


@router.get("/api/trouble-tickets/my-tickets")
async def my_tickets(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List current user's tickets."""
    tickets = svc.get_tickets_by_user(db=db, user_id=user.id)
    return {
        "items": [
            {
                "id": t.id,
                "ticket_number": t.ticket_number,
                "title": t.title,
                "status": t.status,
                "risk_tier": t.risk_tier,
                "category": t.category,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in tickets
        ]
    }


@router.get("/api/trouble-tickets")
async def list_tickets(
    status: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List all tickets (admin only). Optional status filter and pagination."""
    return svc.list_tickets(db=db, status_filter=status, limit=limit, offset=offset)


@router.get("/api/trouble-tickets/{ticket_id}")
async def get_ticket(
    ticket_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get a single ticket. Admin sees any; users see only their own."""
    ticket = svc.get_ticket(db=db, ticket_id=ticket_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    if user.role != "admin" and ticket.submitted_by != user.id:
        raise HTTPException(403, "Access denied")
    submitter = db.get(User, ticket.submitted_by) if ticket.submitted_by else None
    return {
        "id": ticket.id,
        "ticket_number": ticket.ticket_number,
        "title": ticket.title,
        "description": ticket.description,
        "status": ticket.status,
        "risk_tier": ticket.risk_tier,
        "category": ticket.category,
        "submitted_by": ticket.submitted_by,
        "submitted_by_name": submitter.name if submitter else None,
        "current_page": ticket.current_page,
        "auto_captured_context": ticket.auto_captured_context,
        "sanitized_context": ticket.sanitized_context,
        "diagnosis": ticket.diagnosis,
        "generated_prompt": ticket.generated_prompt,
        "file_mapping": ticket.file_mapping,
        "fix_branch": ticket.fix_branch,
        "fix_pr_url": ticket.fix_pr_url,
        "iterations_used": ticket.iterations_used,
        "cost_usd": ticket.cost_usd,
        "resolution_notes": ticket.resolution_notes,
        "parent_ticket_id": ticket.parent_ticket_id,
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
        "diagnosed_at": ticket.diagnosed_at.isoformat() if ticket.diagnosed_at else None,
        "resolved_at": ticket.resolved_at.isoformat() if ticket.resolved_at else None,
    }


@router.patch("/api/trouble-tickets/{ticket_id}")
async def update_ticket(
    ticket_id: int,
    body: TroubleTicketUpdate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Update a ticket (admin only)."""
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(400, "No fields to update")
    ticket = svc.update_ticket(db=db, ticket_id=ticket_id, **updates)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    return {"ok": True}


@router.post("/api/trouble-tickets/{ticket_id}/verify")
async def verify_ticket(
    ticket_id: int,
    body: dict,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """User confirms fix works or reports still broken."""
    ticket = svc.get_ticket(db=db, ticket_id=ticket_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    if ticket.submitted_by != user.id and user.role != "admin":
        raise HTTPException(403, "Access denied")
    if ticket.status != "awaiting_verification":
        raise HTTPException(400, "Ticket is not awaiting verification")

    is_fixed = body.get("is_fixed", True)

    if is_fixed:
        svc.update_ticket(
            db=db, ticket_id=ticket_id,
            status="resolved", resolution_notes="User verified fix",
        )
        return {"ok": True, "status": "resolved"}
    else:
        parent_risk = ticket.risk_tier or "low"
        child_risk = "high" if parent_risk == "high" else "medium"
        child_desc = body.get("description", f"Follow-up: {ticket.title}")
        child = svc.create_ticket(
            db=db,
            user_id=user.id,
            title=f"Follow-up: {ticket.title}",
            description=child_desc,
        )
        svc.update_ticket(
            db=db, ticket_id=child.id,
            risk_tier=child_risk, parent_ticket_id=ticket.id,
        )
        svc.update_ticket(
            db=db, ticket_id=ticket_id,
            status="escalated", resolution_notes="User reported still broken",
        )
        return {"ok": True, "status": "escalated", "child_ticket_id": child.id}
```

**Step 2: Register the router in main.py**

Add at the end of `app/main.py` after the last `app.include_router(...)` block:

```python
from .routers.trouble_tickets import router as trouble_tickets_router

app.include_router(trouble_tickets_router)
```

**Step 3: Run all tests**

Run: `cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_trouble_tickets.py -v --tb=short`
Expected: All tests pass.

**Step 4: Run full suite to check regressions**

Run: `cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ --tb=short -q 2>&1 | tail -10`
Expected: All existing tests still pass.

**Step 5: Commit**

```bash
git add app/routers/trouble_tickets.py app/main.py
git commit -m "feat: add trouble ticket router with CRUD and verify endpoints"
```

---

### Task 5: SPA UI (tickets.js) + Navigation

**Files:**
- Create: `app/static/tickets.js`
- Modify: `app/templates/index.html`

**Step 1: Create tickets.js**

Create `app/static/tickets.js` with the SPA views for submit form, my-tickets list, and admin dashboard. Key requirements:

- HTML-escape all user-provided text using a helper: `function esc(s){const d=document.createElement('div');d.textContent=s;return d.textContent;}` — apply to title, description, submitted_by_name, resolution_notes before inserting into DOM
- Error capture JS (console.error override + window error listener) at top of file
- Common Issues quick-select dropdown
- Submit form with title, description, hidden current_page + frontend_errors
- My Tickets view with status badges and verify buttons
- Admin Dashboard with filter pills, ticket table, and detail view with collapsible context/diagnosis/prompt sections
- Copy-to-clipboard for generated prompts
- All render functions exposed to global scope via `window.renderSubmitTicket = renderSubmitTicket` etc.

See the design doc for full UI specifications. Use textContent or the esc() helper for all user data — never raw innerHTML with user text.

**Step 2: Modify index.html**

Add to `app/templates/index.html`:

1. Script tag: `<script src="/static/tickets.js"></script>` alongside other JS includes
2. Nav pills (find the `fpills` div):
   - All users: `<button type="button" class="fp" onclick="renderSubmitTicket(document.getElementById('view-list'))">Report Issue</button>`
   - All users: `<button type="button" class="fp" onclick="renderMyTickets(document.getElementById('view-list'))">My Tickets</button>`
   - Admin only (wrap in `{% if is_admin %}`): `<button type="button" class="fp" onclick="renderTicketDashboard(document.getElementById('view-list'))">Tickets</button>`

**Step 3: Commit**

```bash
git add app/static/tickets.js app/templates/index.html
git commit -m "feat: add trouble ticket SPA views and navigation"
```

---

### Task 6: Alembic Migration

**Files:**
- Create: `alembic/versions/039_add_trouble_tickets.py`

**Step 1: Create the migration file**

Create `alembic/versions/039_add_trouble_tickets.py`:

```python
"""Add trouble_tickets table for self-heal pipeline.

Revision ID: 039_add_trouble_tickets
Revises: 038_api_health_monitoring
Create Date: 2026-03-01
"""

import sqlalchemy as sa
from alembic import op

revision = "039_add_trouble_tickets"
down_revision = "038_api_health_monitoring"


def upgrade() -> None:
    op.create_table(
        "trouble_tickets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticket_number", sa.String(20), unique=True, nullable=False),
        sa.Column("submitted_by", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(30), nullable=False, server_default="submitted"),
        sa.Column("risk_tier", sa.String(10)),
        sa.Column("category", sa.String(20)),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("current_page", sa.String(500)),
        sa.Column("user_agent", sa.String(500)),
        sa.Column("auto_captured_context", sa.JSON()),
        sa.Column("sanitized_context", sa.JSON()),
        sa.Column("diagnosis", sa.JSON()),
        sa.Column("generated_prompt", sa.Text()),
        sa.Column("file_mapping", sa.JSON()),
        sa.Column("fix_branch", sa.String(200)),
        sa.Column("fix_pr_url", sa.String(500)),
        sa.Column("iterations_used", sa.Integer()),
        sa.Column("cost_tokens", sa.Integer()),
        sa.Column("cost_usd", sa.Float()),
        sa.Column("resolution_notes", sa.Text()),
        sa.Column("parent_ticket_id", sa.Integer(),
                  sa.ForeignKey("trouble_tickets.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
        sa.Column("diagnosed_at", sa.DateTime()),
        sa.Column("resolved_at", sa.DateTime()),
    )
    op.create_index("ix_trouble_tickets_status", "trouble_tickets", ["status"])
    op.create_index("ix_trouble_tickets_risk_tier", "trouble_tickets", ["risk_tier"])
    op.create_index("ix_trouble_tickets_submitted_by", "trouble_tickets", ["submitted_by"])
    op.create_index("ix_trouble_tickets_created_at", "trouble_tickets", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_trouble_tickets_created_at")
    op.drop_index("ix_trouble_tickets_submitted_by")
    op.drop_index("ix_trouble_tickets_risk_tier")
    op.drop_index("ix_trouble_tickets_status")
    op.drop_table("trouble_tickets")
```

**Step 2: Commit (DO NOT run migration)**

```bash
git add alembic/versions/039_add_trouble_tickets.py
git commit -m "migration: 039 add trouble_tickets table"
```

To apply later: `cd /root/availai && alembic upgrade head`

---

### Task 7: Full Test Suite + Coverage Check

**Step 1: Run trouble ticket tests**

Run: `cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_trouble_tickets.py -v --tb=short`
Expected: All pass.

**Step 2: Full suite regression check**

Run: `cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ --tb=short -q 2>&1 | tail -10`
Expected: All existing tests still pass.

**Step 3: Coverage check**

Run: `cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q 2>&1 | tail -20`
Expected: Coverage >= 95%.

**Step 4: Final Phase 1 commit**

```bash
git add -A
git commit -m "feat: Phase 1 complete — trouble ticket system with model, service, router, SPA UI, migration 039"
```

---

## PHASE 2: AI Diagnosis + Risk Classification

_(Tasks 8-13 — execute after Phase 1 is verified)_

### Task 8: Self-Heal Log Model + Migration

**Files:**
- Create: `app/models/self_heal_log.py`
- Modify: `app/models/__init__.py`
- Create: `alembic/versions/040_add_self_heal_log.py`

SelfHealLog model: id, ticket_id (FK), category, risk_tier, files_modified (JSON), fix_succeeded (Boolean nullable), iterations_used (Integer nullable), cost_usd (Float nullable), user_verified (Boolean nullable), created_at. Append-only table.

### Task 9: File Mapper Service + Tests

**Files:**
- Create: `app/services/file_mapper.py`
- Create: `tests/test_file_mapper.py`

Scan `app/routers/` for route definitions, map routes to router/service/template/model files. STABLE_FILES constant. `get_relevant_files(route_pattern, error_context)` returns tagged file list with confidence levels.

### Task 10: Diagnosis Service + Tests

**Files:**
- Create: `app/services/diagnosis_service.py`
- Create: `tests/test_diagnosis_service.py`

Two-stage: classification via `claude_structured()` with `model_tier="smart"`, then detailed diagnosis via `claude_json()`. Risk overrides: confidence < 0.6 bumps tier, STABLE_FILES forces high, REQUIRES_MIGRATION forces high, complex+low bumps to medium. All Claude calls mocked in tests.

### Task 11: Diagnosis Router Endpoint + Config

**Files:**
- Modify: `app/routers/trouble_tickets.py` (add POST diagnose endpoint)
- Modify: `app/config.py` (add SELF_HEAL_AUTO_DIAGNOSE)

### Task 12: Dashboard Diagnosis UI

**Files:**
- Modify: `app/static/tickets.js` (add Diagnose button, diagnosis display)

### Task 13: Phase 2 Tests + Commit

Run full suite, check coverage, commit.

---

## PHASE 3: Prompt Templates + Notifications

_(Tasks 14-20 — execute after Phase 2)_

### Task 14: Prompt Template Engine + Tests
### Task 15: Notification Model + Migration 041
### Task 16: Notification Service + Tests
### Task 17: Notification Router
### Task 18: Notification UI (bell icon, polling)
### Task 19: Dashboard Enhancements (approve/reject, queue, stats)
### Task 20: Phase 3 Tests + Commit

---

## PHASE 4: Local Execution Pipeline (Pluggable Backend)

_(Tasks 21-27 — execute after Phase 3)_

### Task 21: Self-Heal Config Values
### Task 22: Cost Controller + Tests
### Task 23: Execution Service (local subprocess) + Tests
### Task 24: (REMOVED — no GH Actions in v1, local subprocess instead)
### Task 25: Approval/Reject/Execute Endpoints
### Task 26: Rollback Service (alert-only, no auto-revert) + Tests
### Task 27: Phase 4 Tests + Commit

---

## PHASE 5: Weekly Reports + Polish

_(Tasks 28-33 — execute after Phase 4)_

### Task 28: Pattern Tracker + Tests
### Task 29: Scheduler Integration (weekly report, auto-close)
### Task 30: Report UI
### Task 31: UX Polish (keyboard shortcuts, health indicator, satisfaction survey)
### Task 32: Full Integration Tests (3 lifecycle scenarios)
### Task 33: Final Commit + Deployment Checklist
