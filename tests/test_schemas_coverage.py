"""Tests for Pydantic schema files with 0% coverage.

Covers: buy_plan, knowledge, rfq, task, prospect_pool schemas.

Called by: pytest
Depends on: app/schemas/*
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.schemas.buy_plan import (
    BuyPlanLineEdit,
    BuyPlanLineIssue,
    BuyPlanLineOverride,
    BuyPlanTokenApproval,
    BuyPlanTokenReject,
    POConfirmation,
    POVerificationRequest,
    SOVerificationRequest,
    VerificationGroupUpdate,
)
from app.schemas.knowledge import (
    AnswerCreate,
    KnowledgeEntryCreate,
    KnowledgeEntryResponse,
    KnowledgeEntryUpdate,
    QuestionCreate,
)
from app.schemas.prospect_pool import (
    PoolAccountList,
    PoolAccountRead,
    PoolDismissRequest,
    PoolFilters,
    PoolStats,
)
from app.schemas.rfq import (
    BatchRfqSend,
    FollowUpEmail,
    PhoneCallLog,
    RfqPrepare,
    RfqPrepareVendor,
    RfqVendorGroup,
    VendorResponseStatusUpdate,
)
from app.schemas.task import (
    TaskComplete,
    TaskCreate,
    TaskStatusUpdate,
    TaskUpdate,
)

# ── Buy Plan Schemas ────────────────────────────────────────────────


class TestBuyPlanLineEdit:
    def test_valid(self):
        s = BuyPlanLineEdit(requirement_id=1, offer_id=2, quantity=10)
        assert s.requirement_id == 1
        assert s.offer_id == 2
        assert s.quantity == 10
        assert s.sales_note is None

    def test_with_sales_note(self):
        s = BuyPlanLineEdit(requirement_id=1, offer_id=2, quantity=5, sales_note="rush")
        assert s.sales_note == "rush"

    @pytest.mark.parametrize("quantity", [0, -1], ids=["zero", "negative"])
    def test_quantity_must_be_positive(self, quantity):
        with pytest.raises(ValidationError):
            BuyPlanLineEdit(requirement_id=1, offer_id=2, quantity=quantity)

    def test_missing_required_fields(self):
        with pytest.raises(ValidationError):
            BuyPlanLineEdit()


class TestBuyPlanLineOverride:
    def test_minimal(self):
        s = BuyPlanLineOverride(line_id=5)
        assert s.line_id == 5
        assert s.offer_id is None
        assert s.quantity is None
        assert s.manager_note is None

    def test_full(self):
        s = BuyPlanLineOverride(line_id=5, offer_id=10, quantity=3, manager_note="swap vendor")
        assert s.offer_id == 10
        assert s.quantity == 3
        assert s.manager_note == "swap vendor"

    def test_quantity_gt_zero_when_provided(self):
        with pytest.raises(ValidationError):
            BuyPlanLineOverride(line_id=5, quantity=0)


class TestSOVerificationRequest:
    def test_approve(self):
        s = SOVerificationRequest(action="approve")
        assert s.action == "approve"
        assert s.rejection_note is None

    @pytest.mark.parametrize(
        ("action", "raw_note", "expected"),
        [
            ("reject", "  wrong SO  ", "wrong SO"),
            ("halt", "on hold", "on hold"),
            ("approve", "  info  ", "info"),
        ],
        ids=["reject_with_note", "halt_with_note", "approve_with_note_strips"],
    )
    def test_note_stripped(self, action, raw_note, expected):
        s = SOVerificationRequest(action=action, rejection_note=raw_note)
        assert s.rejection_note == expected

    @pytest.mark.parametrize("action", ["reject", "halt"])
    def test_requires_note(self, action):
        with pytest.raises(ValidationError, match="note is required"):
            SOVerificationRequest(action=action)

    def test_reject_blank_note_rejected(self):
        with pytest.raises(ValidationError):
            SOVerificationRequest(action="reject", rejection_note="   ")

    def test_invalid_action(self):
        with pytest.raises(ValidationError):
            SOVerificationRequest(action="cancel")


class TestPOConfirmation:
    def test_valid(self):
        dt = datetime(2026, 6, 1, tzinfo=timezone.utc)
        s = POConfirmation(po_number="PO-12345", estimated_ship_date=dt)
        assert s.po_number == "PO-12345"
        assert s.estimated_ship_date == dt

    def test_po_number_stripped(self):
        dt = datetime(2026, 6, 1, tzinfo=timezone.utc)
        s = POConfirmation(po_number="  PO-999  ", estimated_ship_date=dt)
        assert s.po_number == "PO-999"

    @pytest.mark.parametrize("po_number", ["   ", ""], ids=["blank", "empty"])
    def test_missing_po_number(self, po_number):
        dt = datetime(2026, 6, 1, tzinfo=timezone.utc)
        with pytest.raises(ValidationError, match="PO number is required"):
            POConfirmation(po_number=po_number, estimated_ship_date=dt)


class TestPOVerificationRequest:
    def test_approve(self):
        s = POVerificationRequest(action="approve")
        assert s.action == "approve"

    @pytest.mark.parametrize(
        ("action", "raw_note", "expected"),
        [
            ("reject", "wrong amount", "wrong amount"),
            ("approve", "  fyi  ", "fyi"),
        ],
        ids=["reject_with_note", "approve_with_note_strips"],
    )
    def test_note_stripped(self, action, raw_note, expected):
        s = POVerificationRequest(action=action, rejection_note=raw_note)
        assert s.rejection_note == expected

    def test_reject_without_note(self):
        with pytest.raises(ValidationError, match="note is required"):
            POVerificationRequest(action="reject")

    def test_reject_blank_note(self):
        with pytest.raises(ValidationError):
            POVerificationRequest(action="reject", rejection_note="  ")

    def test_invalid_action(self):
        with pytest.raises(ValidationError):
            POVerificationRequest(action="halt")


class TestBuyPlanLineIssue:
    def test_sold_out(self):
        s = BuyPlanLineIssue(issue_type="sold_out")
        assert s.issue_type == "sold_out"
        assert s.note is None

    @pytest.mark.parametrize(
        ("issue_type", "raw_note", "expected"),
        [
            ("other", "custom issue", "custom issue"),
            ("price_changed", "went up 10%", "went up 10%"),
            ("other", "  spaces  ", "spaces"),
        ],
        ids=["other_with_note", "price_changed", "note_stripped"],
    )
    def test_note_round_trip(self, issue_type, raw_note, expected):
        s = BuyPlanLineIssue(issue_type=issue_type, note=raw_note)
        assert s.note == expected

    def test_other_requires_note(self):
        with pytest.raises(ValidationError, match="note is required"):
            BuyPlanLineIssue(issue_type="other")

    def test_other_blank_note(self):
        with pytest.raises(ValidationError):
            BuyPlanLineIssue(issue_type="other", note="   ")

    def test_lead_time_changed(self):
        s = BuyPlanLineIssue(issue_type="lead_time_changed")
        assert s.issue_type == "lead_time_changed"

    def test_invalid_type(self):
        with pytest.raises(ValidationError):
            BuyPlanLineIssue(issue_type="damaged")


class TestVerificationGroupUpdate:
    @pytest.mark.parametrize(("user_id", "action"), [(42, "add"), (1, "remove")])
    def test_valid_actions(self, user_id, action):
        s = VerificationGroupUpdate(user_id=user_id, action=action)
        assert s.user_id == user_id
        assert s.action == action

    def test_invalid_action(self):
        with pytest.raises(ValidationError):
            VerificationGroupUpdate(user_id=1, action="toggle")


class TestBuyPlanTokenApproval:
    def test_valid(self):
        s = BuyPlanTokenApproval(sales_order_number="SO-100")
        assert s.sales_order_number == "SO-100"
        assert s.notes is None

    def test_with_notes(self):
        s = BuyPlanTokenApproval(sales_order_number="SO-100", notes="approved")
        assert s.notes == "approved"


class TestBuyPlanTokenReject:
    def test_default_reason(self):
        s = BuyPlanTokenReject()
        assert s.reason == ""

    def test_with_reason(self):
        s = BuyPlanTokenReject(reason="pricing too high")
        assert s.reason == "pricing too high"


# ── Knowledge Schemas ───────────────────────────────────────────────


class TestKnowledgeEntryCreate:
    def test_valid_minimal(self):
        s = KnowledgeEntryCreate(entry_type="fact", content="Test content")
        assert s.entry_type == "fact"
        assert s.content == "Test content"
        assert s.source == "manual"
        assert s.confidence is None
        assert s.expires_at is None

    def test_all_entry_types(self):
        for t in ("question", "answer", "fact", "note", "ai_insight"):
            s = KnowledgeEntryCreate(entry_type=t, content="x")
            assert s.entry_type == t

    def test_invalid_entry_type(self):
        with pytest.raises(ValidationError):
            KnowledgeEntryCreate(entry_type="invalid", content="x")

    def test_all_sources(self):
        for src in ("manual", "ai_generated", "system", "email_parsed", "teams_bot"):
            s = KnowledgeEntryCreate(entry_type="fact", content="x", source=src)
            assert s.source == src

    def test_invalid_source(self):
        with pytest.raises(ValidationError):
            KnowledgeEntryCreate(entry_type="fact", content="x", source="twitter")

    @pytest.mark.parametrize("content", ["", "x" * 10001], ids=["min_length", "max_length"])
    def test_invalid_content_length(self, content):
        with pytest.raises(ValidationError):
            KnowledgeEntryCreate(entry_type="fact", content=content)

    @pytest.mark.parametrize("confidence", [0.5, 0.0, 1.0], ids=["range", "zero", "one"])
    def test_confidence_valid(self, confidence):
        s = KnowledgeEntryCreate(entry_type="fact", content="x", confidence=confidence)
        assert s.confidence == confidence

    @pytest.mark.parametrize("confidence", [1.1, -0.1], ids=["too_high", "negative"])
    def test_confidence_out_of_range(self, confidence):
        with pytest.raises(ValidationError):
            KnowledgeEntryCreate(entry_type="fact", content="x", confidence=confidence)

    def test_optional_foreign_keys(self):
        s = KnowledgeEntryCreate(
            entry_type="fact",
            content="x",
            mpn="LM358",
            vendor_card_id=1,
            company_id=2,
            requisition_id=3,
            requirement_id=4,
        )
        assert s.mpn == "LM358"
        assert s.vendor_card_id == 1
        assert s.company_id == 2
        assert s.requisition_id == 3
        assert s.requirement_id == 4


class TestQuestionCreate:
    def test_valid(self):
        s = QuestionCreate(content="What is MOQ?", assigned_to_ids=[1, 2])
        assert s.content == "What is MOQ?"
        assert s.assigned_to_ids == [1, 2]

    def test_empty_content(self):
        with pytest.raises(ValidationError):
            QuestionCreate(content="", assigned_to_ids=[1])

    def test_empty_assigned_to(self):
        with pytest.raises(ValidationError):
            QuestionCreate(content="question", assigned_to_ids=[])

    def test_optional_fields(self):
        s = QuestionCreate(
            content="q",
            assigned_to_ids=[1],
            mpn="ABC",
            vendor_card_id=5,
            company_id=6,
            requisition_id=7,
            requirement_id=8,
        )
        assert s.mpn == "ABC"


class TestAnswerCreate:
    def test_valid(self):
        s = AnswerCreate(content="The MOQ is 100")
        assert s.content == "The MOQ is 100"

    @pytest.mark.parametrize("content", ["", "x" * 10001], ids=["empty", "too_long"])
    def test_invalid_content_length(self, content):
        with pytest.raises(ValidationError):
            AnswerCreate(content=content)


class TestKnowledgeEntryUpdate:
    def test_all_none(self):
        s = KnowledgeEntryUpdate()
        assert s.content is None
        assert s.is_resolved is None
        assert s.expires_at is None

    def test_partial_update(self):
        s = KnowledgeEntryUpdate(content="updated", is_resolved=True)
        assert s.content == "updated"
        assert s.is_resolved is True

    def test_content_min_length(self):
        with pytest.raises(ValidationError):
            KnowledgeEntryUpdate(content="")


class TestKnowledgeEntryResponse:
    def test_valid(self):
        now = datetime.now(timezone.utc)
        s = KnowledgeEntryResponse(
            id=1,
            entry_type="fact",
            content="test",
            source="manual",
            created_at=now,
            updated_at=now,
        )
        assert s.id == 1
        assert s.confidence is None
        assert s.is_expired is False
        assert s.is_resolved is False
        assert s.answers == []
        assert s.assigned_to_ids == []

    def test_extra_fields_allowed(self):
        now = datetime.now(timezone.utc)
        s = KnowledgeEntryResponse(
            id=1,
            entry_type="fact",
            content="test",
            source="manual",
            created_at=now,
            updated_at=now,
            custom_field="extra",
        )
        assert s.custom_field == "extra"

    def test_nested_answers(self):
        now = datetime.now(timezone.utc)
        answer = KnowledgeEntryResponse(
            id=2,
            entry_type="answer",
            content="reply",
            source="manual",
            created_at=now,
            updated_at=now,
        )
        s = KnowledgeEntryResponse(
            id=1,
            entry_type="question",
            content="q",
            source="manual",
            created_at=now,
            updated_at=now,
            answers=[answer],
        )
        assert len(s.answers) == 1
        assert s.answers[0].id == 2


# ── RFQ Schemas ─────────────────────────────────────────────────────


class TestPhoneCallLog:
    def test_valid(self):
        s = PhoneCallLog(requisition_id=1, vendor_name="Acme", vendor_phone="555-1234")
        assert s.requisition_id == 1
        assert s.vendor_name == "Acme"
        assert s.parts == []

    def test_with_parts(self):
        s = PhoneCallLog(
            requisition_id=1,
            vendor_name="Acme",
            vendor_phone="555",
            parts=["LM358", "NE555"],
        )
        assert s.parts == ["LM358", "NE555"]

    @pytest.mark.parametrize(
        ("vendor_name", "vendor_phone"),
        [("   ", "555"), ("Acme", "  ")],
        ids=["blank_vendor_name", "blank_vendor_phone"],
    )
    def test_blank_fields_rejected(self, vendor_name, vendor_phone):
        with pytest.raises(ValidationError, match="must not be blank"):
            PhoneCallLog(requisition_id=1, vendor_name=vendor_name, vendor_phone=vendor_phone)

    def test_strips_whitespace(self):
        s = PhoneCallLog(requisition_id=1, vendor_name="  Acme  ", vendor_phone=" 555 ")
        assert s.vendor_name == "Acme"
        assert s.vendor_phone == "555"


class TestRfqVendorGroup:
    def test_valid(self):
        s = RfqVendorGroup(vendor_name="Acme", vendor_email="a@b.com")
        assert s.vendor_name == "Acme"
        assert s.parts == []
        assert s.subject == ""
        assert s.body == ""

    def test_invalid_email(self):
        with pytest.raises(ValidationError, match="Invalid email"):
            RfqVendorGroup(vendor_name="Acme", vendor_email="nope")

    def test_with_parts_and_body(self):
        s = RfqVendorGroup(
            vendor_name="X",
            vendor_email="x@y.com",
            parts=["A", "B"],
            subject="RFQ",
            body="Hello",
        )
        assert s.parts == ["A", "B"]
        assert s.subject == "RFQ"


class TestBatchRfqSend:
    def test_valid(self):
        group = RfqVendorGroup(vendor_name="A", vendor_email="a@b.com")
        s = BatchRfqSend(groups=[group])
        assert len(s.groups) == 1

    def test_empty_groups_rejected(self):
        with pytest.raises(ValidationError):
            BatchRfqSend(groups=[])


class TestRfqPrepareVendor:
    def test_valid(self):
        s = RfqPrepareVendor(vendor_name="Acme")
        assert s.vendor_name == "Acme"


class TestRfqPrepare:
    def test_empty_vendors(self):
        s = RfqPrepare()
        assert s.vendors == []

    def test_with_vendors(self):
        s = RfqPrepare(vendors=[RfqPrepareVendor(vendor_name="A")])
        assert len(s.vendors) == 1


class TestFollowUpEmail:
    def test_default_empty(self):
        s = FollowUpEmail()
        assert s.body == ""

    def test_with_body(self):
        s = FollowUpEmail(body="Please respond")
        assert s.body == "Please respond"


class TestVendorResponseStatusUpdate:
    def test_valid_statuses(self):
        for status in ("new", "reviewed", "rejected"):
            s = VendorResponseStatusUpdate(status=status)
            assert s.status == status

    def test_strips_and_lowercases(self):
        s = VendorResponseStatusUpdate(status="  REVIEWED  ")
        assert s.status == "reviewed"

    def test_invalid_status(self):
        with pytest.raises(ValidationError, match="Status must be one of"):
            VendorResponseStatusUpdate(status="approved")


# ── Task Schemas ────────────────────────────────────────────────────


class TestTaskCreate:
    def test_valid(self):
        future = datetime.now(timezone.utc) + timedelta(hours=48)
        s = TaskCreate(title="Fix bug", assigned_to_id=1, due_at=future)
        assert s.title == "Fix bug"
        assert s.description is None
        assert s.assigned_to_id == 1

    def test_due_at_too_soon(self):
        soon = datetime.now(timezone.utc) + timedelta(hours=1)
        with pytest.raises(ValidationError, match="at least 24 hours"):
            TaskCreate(title="X", assigned_to_id=1, due_at=soon)

    @pytest.mark.parametrize("title", ["", "x" * 256], ids=["min_length", "max_length"])
    def test_invalid_title_length(self, title):
        future = datetime.now(timezone.utc) + timedelta(hours=48)
        with pytest.raises(ValidationError):
            TaskCreate(title=title, assigned_to_id=1, due_at=future)

    def test_naive_datetime_treated_as_utc(self):
        future_naive = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=48)
        s = TaskCreate(title="X", assigned_to_id=1, due_at=future_naive)
        assert s.due_at == future_naive

    def test_with_description(self):
        future = datetime.now(timezone.utc) + timedelta(hours=48)
        s = TaskCreate(title="T", assigned_to_id=1, due_at=future, description="details")
        assert s.description == "details"


class TestTaskUpdate:
    def test_all_none(self):
        s = TaskUpdate()
        assert s.title is None
        assert s.description is None
        assert s.assigned_to_id is None
        assert s.due_at is None

    def test_partial(self):
        s = TaskUpdate(title="New title")
        assert s.title == "New title"

    def test_due_at_none_passes(self):
        s = TaskUpdate(due_at=None)
        assert s.due_at is None

    def test_due_at_too_soon(self):
        soon = datetime.now(timezone.utc) + timedelta(hours=1)
        with pytest.raises(ValidationError, match="at least 24 hours"):
            TaskUpdate(due_at=soon)

    def test_due_at_valid(self):
        future = datetime.now(timezone.utc) + timedelta(hours=48)
        s = TaskUpdate(due_at=future)
        assert s.due_at == future

    def test_title_min_length(self):
        with pytest.raises(ValidationError):
            TaskUpdate(title="")


class TestTaskComplete:
    def test_valid(self):
        s = TaskComplete(completion_note="Done via email")
        assert s.completion_note == "Done via email"

    def test_empty_note(self):
        with pytest.raises(ValidationError):
            TaskComplete(completion_note="")


class TestTaskStatusUpdate:
    def test_valid_statuses(self):
        for status in ("todo", "in_progress", "done"):
            s = TaskStatusUpdate(status=status)
            assert s.status == status

    def test_strips_and_lowercases(self):
        s = TaskStatusUpdate(status="  IN_PROGRESS  ")
        assert s.status == "in_progress"

    def test_invalid_status(self):
        with pytest.raises(ValidationError, match="Invalid status"):
            TaskStatusUpdate(status="cancelled")


# ── Prospect Pool Schemas ───────────────────────────────────────────


class TestPoolAccountRead:
    def test_minimal(self):
        s = PoolAccountRead(id=1, name="Acme Corp")
        assert s.id == 1
        assert s.name == "Acme Corp"
        assert s.domain is None
        assert s.website is None
        assert s.industry is None
        assert s.phone is None
        assert s.hq_city is None
        assert s.hq_state is None
        assert s.hq_country is None
        assert s.import_priority is None
        assert s.sf_account_id is None

    def test_full(self):
        s = PoolAccountRead(
            id=1,
            name="Acme",
            domain="acme.com",
            website="https://acme.com",
            industry="Electronics",
            phone="555-0100",
            hq_city="Austin",
            hq_state="TX",
            hq_country="US",
            import_priority="high",
            sf_account_id="SF123",
        )
        assert s.domain == "acme.com"
        assert s.hq_state == "TX"


class TestPoolStats:
    def test_defaults(self):
        s = PoolStats()
        assert s.total_available == 0
        assert s.priority_count == 0
        assert s.standard_count == 0
        assert s.claimed_this_month == 0

    def test_with_values(self):
        s = PoolStats(total_available=100, priority_count=20, standard_count=80, claimed_this_month=5)
        assert s.total_available == 100


class TestPoolAccountList:
    def test_valid(self):
        account = PoolAccountRead(id=1, name="A")
        stats = PoolStats()
        s = PoolAccountList(items=[account], total=1, page=1, per_page=20, pool_stats=stats)
        assert len(s.items) == 1
        assert s.total == 1

    def test_empty_items(self):
        s = PoolAccountList(items=[], total=0, page=1, per_page=20, pool_stats=PoolStats())
        assert s.items == []


class TestPoolDismissRequest:
    def test_valid_reasons(self):
        for reason in ("not_relevant", "competitor", "too_small", "too_large", "duplicate", "other"):
            s = PoolDismissRequest(reason=reason)
            assert s.reason == reason

    def test_invalid_reason(self):
        with pytest.raises(ValidationError):
            PoolDismissRequest(reason="spam")


class TestPoolFilters:
    def test_defaults(self):
        s = PoolFilters()
        assert s.import_priority is None
        assert s.industry is None
        assert s.search is None
        assert s.sort_by == "priority"
        assert s.page == 1
        assert s.per_page == 20

    def test_custom_values(self):
        s = PoolFilters(
            import_priority="high", industry="Electronics", search="acme", sort_by="name", page=3, per_page=50
        )
        assert s.import_priority == "high"
        assert s.page == 3
        assert s.per_page == 50

    @pytest.mark.parametrize(
        "kwargs",
        [{"page": 0}, {"per_page": 0}, {"per_page": 101}],
        ids=["page_minimum", "per_page_minimum", "per_page_maximum"],
    )
    def test_bounds_rejected(self, kwargs):
        with pytest.raises(ValidationError):
            PoolFilters(**kwargs)
