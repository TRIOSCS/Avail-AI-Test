"""test_coverage_schemas.py — Tests for schema modules below 85% coverage.

Covers: app/schemas/sourcing_leads.py, app/schemas/task.py, app/schemas/enrichment.py

Called by: pytest
Depends on: pydantic, app.schemas
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.schemas.enrichment import (
    BackfillRequest,
    BatchEnrichRequest,
    BulkApproveRequest,
    ContactSummary,
    CreditUsageItem,
    CustomerBackfillRequest,
    CustomerEnrichmentResult,
    CustomerEnrichRequest,
    EnrichmentJobSummary,
    EnrichmentQueueItem,
    EnrichmentStats,
    EnrichmentStatusResponse,
    QueueActionRequest,
    VerifyEmailRequest,
)
from app.schemas.sourcing_leads import (
    EvidenceOut,
    FeedbackEventOut,
    LeadDetailOut,
    LeadFeedbackIn,
    LeadOut,
    LeadStatusUpdateIn,
)
from app.schemas.task import (
    TaskComplete,
    TaskCreate,
    TaskStatusUpdate,
    TaskSummary,
    TaskUpdate,
)

# ── Sourcing Leads Schemas ──────────────────────────────────────────────


class TestLeadStatusUpdateIn:
    def test_valid_status_new(self):
        obj = LeadStatusUpdateIn(status="new")
        assert obj.status == "new"

    def test_valid_status_contacted(self):
        obj = LeadStatusUpdateIn(status="contacted")
        assert obj.status == "contacted"

    def test_valid_status_has_stock(self):
        obj = LeadStatusUpdateIn(status="has_stock")
        assert obj.status == "has_stock"

    def test_valid_status_do_not_contact(self):
        obj = LeadStatusUpdateIn(status="do_not_contact")
        assert obj.status == "do_not_contact"

    def test_valid_status_bad_lead(self):
        obj = LeadStatusUpdateIn(status="bad_lead")
        assert obj.status == "bad_lead"

    def test_invalid_status_raises(self):
        with pytest.raises(ValidationError):
            LeadStatusUpdateIn(status="invalid_status")

    def test_with_note(self):
        obj = LeadStatusUpdateIn(status="no_stock", note="Checked stock, nothing available")
        assert obj.note == "Checked stock, nothing available"

    def test_note_too_long_raises(self):
        with pytest.raises(ValidationError):
            LeadStatusUpdateIn(status="new", note="x" * 2001)

    def test_reason_code_max_length(self):
        with pytest.raises(ValidationError):
            LeadStatusUpdateIn(status="new", reason_code="x" * 65)

    def test_contact_method_max_length(self):
        with pytest.raises(ValidationError):
            LeadStatusUpdateIn(status="new", contact_method="x" * 33)

    def test_contact_attempt_count_negative(self):
        with pytest.raises(ValidationError):
            LeadStatusUpdateIn(status="new", contact_attempt_count=-1)

    def test_contact_attempt_count_too_large(self):
        with pytest.raises(ValidationError):
            LeadStatusUpdateIn(status="new", contact_attempt_count=1000)

    def test_defaults(self):
        obj = LeadStatusUpdateIn(status="replied")
        assert obj.note is None
        assert obj.reason_code is None
        assert obj.contact_method is None
        assert obj.contact_attempt_count == 0


class TestLeadFeedbackIn:
    def test_empty_ok(self):
        obj = LeadFeedbackIn()
        assert obj.note is None
        assert obj.contact_attempt_count == 0

    def test_with_all_fields(self):
        obj = LeadFeedbackIn(
            note="Test note",
            reason_code="out_of_stock",
            contact_method="email",
            contact_attempt_count=3,
        )
        assert obj.note == "Test note"
        assert obj.reason_code == "out_of_stock"
        assert obj.contact_method == "email"
        assert obj.contact_attempt_count == 3

    def test_note_too_long(self):
        with pytest.raises(ValidationError):
            LeadFeedbackIn(note="x" * 2001)


class TestEvidenceOut:
    def test_minimal_construction(self):
        obj = EvidenceOut(
            id=1,
            evidence_id="ev-001",
            signal_type="sighting",
            source_type="brokerbin",
            source_name="BrokerBin",
        )
        assert obj.id == 1
        assert obj.evidence_id == "ev-001"
        assert obj.source_reference is None
        assert obj.freshness_age_days is None

    def test_from_attributes(self):
        # Verify from_attributes config works
        assert EvidenceOut.model_config.get("from_attributes") is True


class TestFeedbackEventOut:
    def test_minimal(self):
        obj = FeedbackEventOut(id=1, status="contacted")
        assert obj.id == 1
        assert obj.status == "contacted"
        assert obj.note is None
        assert obj.contact_attempt_count == 0

    def test_full_fields(self):
        now = datetime.now(timezone.utc)
        obj = FeedbackEventOut(
            id=2,
            status="has_stock",
            note="Confirmed 500 units",
            reason_code="confirmed",
            contact_method="phone",
            contact_attempt_count=1,
            created_by_user_id=42,
            created_at=now,
        )
        assert obj.note == "Confirmed 500 units"
        assert obj.created_by_user_id == 42


class TestLeadOut:
    def _base_lead(self, **kwargs):
        base = dict(
            id=1,
            lead_id="lead-abc-123",
            requisition_id=10,
            requirement_id=20,
            vendor_name="Arrow Electronics",
            part_number_requested="LM317T",
            part_number_matched="LM317T",
            confidence_score=0.85,
            confidence_band="high",
            reason_summary="Strong evidence from BrokerBin",
            evidence_count=3,
            corroborated=True,
            buyer_status="new",
        )
        base.update(kwargs)
        return base

    def test_minimal_lead_out(self):
        obj = LeadOut(**self._base_lead())
        assert obj.vendor_name == "Arrow Electronics"
        assert obj.confidence_score == 0.85
        assert obj.corroborated is True
        assert obj.risk_flags == []
        assert obj.vendor_safety_flags == []

    def test_with_contact_info(self):
        obj = LeadOut(
            **self._base_lead(
                contact_name="John Smith",
                contact_email="john@arrow.com",
                contact_phone="+1-555-0100",
            )
        )
        assert obj.contact_name == "John Smith"
        assert obj.contact_email == "john@arrow.com"

    def test_from_attributes_config(self):
        assert LeadOut.model_config.get("from_attributes") is True


class TestLeadDetailOut:
    def test_inherits_from_lead_out(self):
        base = dict(
            id=1,
            lead_id="lead-xyz",
            requisition_id=5,
            requirement_id=10,
            vendor_name="Mouser",
            part_number_requested="BC547",
            part_number_matched="BC547",
            confidence_score=0.7,
            confidence_band="medium",
            reason_summary="Moderate evidence",
            evidence_count=1,
            corroborated=False,
            buyer_status="new",
        )
        obj = LeadDetailOut(**base)
        assert obj.evidence == []
        assert obj.feedback_events == []

    def test_with_evidence_list(self):
        evidence = [
            EvidenceOut(
                id=1,
                evidence_id="ev-001",
                signal_type="sighting",
                source_type="digikey",
                source_name="DigiKey",
            )
        ]
        obj = LeadDetailOut(
            id=1,
            lead_id="ld-1",
            requisition_id=1,
            requirement_id=1,
            vendor_name="DigiKey",
            part_number_requested="LM317",
            part_number_matched="LM317",
            confidence_score=0.9,
            confidence_band="high",
            reason_summary="DigiKey authorized",
            evidence_count=1,
            corroborated=True,
            buyer_status="new",
            evidence=evidence,
        )
        assert len(obj.evidence) == 1


# ── Task Schemas ──────────────────────────────────────────────────────


class TestTaskCreate:
    def test_valid_task(self):
        future = datetime.now(timezone.utc) + timedelta(hours=48)
        obj = TaskCreate(title="Call vendor", assigned_to_id=1, due_at=future)
        assert obj.title == "Call vendor"
        assert obj.assigned_to_id == 1

    def test_title_too_short(self):
        future = datetime.now(timezone.utc) + timedelta(hours=48)
        with pytest.raises(ValidationError):
            TaskCreate(title="", assigned_to_id=1, due_at=future)

    def test_title_too_long(self):
        future = datetime.now(timezone.utc) + timedelta(hours=48)
        with pytest.raises(ValidationError):
            TaskCreate(title="x" * 256, assigned_to_id=1, due_at=future)

    def test_due_at_too_soon(self):
        too_soon = datetime.now(timezone.utc) + timedelta(hours=1)
        with pytest.raises(ValidationError):
            TaskCreate(title="Task", assigned_to_id=1, due_at=too_soon)

    def test_due_at_past(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        with pytest.raises(ValidationError):
            TaskCreate(title="Task", assigned_to_id=1, due_at=past)

    def test_due_at_naive_datetime_too_soon(self):
        """Naive datetime treated as UTC — should still fail if < 24h."""
        soon = datetime.now() + timedelta(hours=2)
        with pytest.raises(ValidationError):
            TaskCreate(title="Task", assigned_to_id=1, due_at=soon)

    def test_with_description(self):
        future = datetime.now(timezone.utc) + timedelta(hours=48)
        obj = TaskCreate(
            title="Send RFQ",
            description="Contact 3 vendors about LM317T",
            assigned_to_id=5,
            due_at=future,
        )
        assert obj.description == "Contact 3 vendors about LM317T"


class TestTaskUpdate:
    def test_all_optional(self):
        obj = TaskUpdate()
        assert obj.title is None
        assert obj.description is None
        assert obj.assigned_to_id is None
        assert obj.due_at is None

    def test_partial_update(self):
        future = datetime.now(timezone.utc) + timedelta(hours=48)
        obj = TaskUpdate(title="Updated Title", due_at=future)
        assert obj.title == "Updated Title"

    def test_due_at_none_allowed(self):
        obj = TaskUpdate(due_at=None)
        assert obj.due_at is None

    def test_due_at_too_soon_rejected(self):
        soon = datetime.now(timezone.utc) + timedelta(hours=2)
        with pytest.raises(ValidationError):
            TaskUpdate(due_at=soon)

    def test_title_empty_rejected(self):
        with pytest.raises(ValidationError):
            TaskUpdate(title="")


class TestTaskComplete:
    def test_valid(self):
        obj = TaskComplete(completion_note="Resolved via phone call")
        assert obj.completion_note == "Resolved via phone call"

    def test_empty_note_rejected(self):
        with pytest.raises(ValidationError):
            TaskComplete(completion_note="")


class TestTaskStatusUpdate:
    def test_valid_todo(self):
        obj = TaskStatusUpdate(status="todo")
        assert obj.status == "todo"

    def test_valid_in_progress(self):
        obj = TaskStatusUpdate(status="in_progress")
        assert obj.status == "in_progress"

    def test_valid_done(self):
        obj = TaskStatusUpdate(status="done")
        assert obj.status == "done"

    def test_strips_whitespace(self):
        obj = TaskStatusUpdate(status="  done  ")
        assert obj.status == "done"

    def test_case_insensitive(self):
        obj = TaskStatusUpdate(status="TODO")
        assert obj.status == "todo"

    def test_invalid_status(self):
        with pytest.raises(ValidationError):
            TaskStatusUpdate(status="completed")

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            TaskStatusUpdate(status="pending")


class TestTaskSummary:
    def test_defaults_zero(self):
        obj = TaskSummary()
        assert obj.assigned_to_me == 0
        assert obj.waiting_on == 0
        assert obj.overdue == 0

    def test_custom_values(self):
        obj = TaskSummary(assigned_to_me=5, waiting_on=2, overdue=1)
        assert obj.assigned_to_me == 5
        assert obj.overdue == 1


# ── Enrichment Schemas ────────────────────────────────────────────────


class TestBackfillRequest:
    def test_defaults(self):
        obj = BackfillRequest()
        assert "vendor" in obj.entity_types
        assert "company" in obj.entity_types
        assert obj.max_items == 500
        assert obj.include_deep_email is False
        assert obj.lookback_days == 365

    def test_max_items_too_small(self):
        with pytest.raises(ValidationError):
            BackfillRequest(max_items=0)

    def test_max_items_too_large(self):
        with pytest.raises(ValidationError):
            BackfillRequest(max_items=2001)

    def test_lookback_days_range(self):
        obj = BackfillRequest(lookback_days=30)
        assert obj.lookback_days == 30

    def test_lookback_days_too_small(self):
        with pytest.raises(ValidationError):
            BackfillRequest(lookback_days=0)

    def test_lookback_days_too_large(self):
        with pytest.raises(ValidationError):
            BackfillRequest(lookback_days=731)


class TestQueueActionRequest:
    def test_empty_ok(self):
        obj = QueueActionRequest()
        assert obj is not None


class TestBulkApproveRequest:
    def test_valid_ids(self):
        obj = BulkApproveRequest(ids=[1, 2, 3])
        assert obj.ids == [1, 2, 3]

    def test_empty_ids_rejected(self):
        with pytest.raises(ValidationError):
            BulkApproveRequest(ids=[])

    def test_too_many_ids_rejected(self):
        with pytest.raises(ValidationError):
            BulkApproveRequest(ids=list(range(501)))


class TestEnrichmentQueueItem:
    def test_construction(self):
        obj = EnrichmentQueueItem(
            id=1,
            enrichment_type="website",
            field_name="website",
            proposed_value="https://acme.com",
            confidence=0.9,
            source="web_scrape",
            status="pending",
        )
        assert obj.id == 1
        assert obj.entity_type is None
        assert obj.confidence == 0.9


class TestEnrichmentJobSummary:
    def test_construction(self):
        obj = EnrichmentJobSummary(
            id=1,
            job_type="vendor_backfill",
            status="running",
            total_items=100,
            processed_items=50,
            enriched_items=45,
            error_count=5,
            progress_pct=50.0,
        )
        assert obj.progress_pct == 50.0
        assert obj.started_by is None


class TestEnrichmentStats:
    def test_defaults(self):
        obj = EnrichmentStats()
        assert obj.queue_pending == 0
        assert obj.vendors_enriched == 0
        assert obj.active_jobs == 0

    def test_custom_values(self):
        obj = EnrichmentStats(queue_pending=10, vendors_enriched=42, active_jobs=2)
        assert obj.vendors_enriched == 42


class TestCustomerEnrichRequest:
    def test_default_force_false(self):
        obj = CustomerEnrichRequest()
        assert obj.force is False

    def test_force_true(self):
        obj = CustomerEnrichRequest(force=True)
        assert obj.force is True


class TestVerifyEmailRequest:
    def test_valid_email(self):
        obj = VerifyEmailRequest(email="user@example.com")
        assert obj.email == "user@example.com"

    def test_too_short(self):
        with pytest.raises(ValidationError):
            VerifyEmailRequest(email="ab")


class TestCustomerBackfillRequest:
    def test_defaults(self):
        obj = CustomerBackfillRequest()
        assert obj.max_accounts == 50
        assert obj.assigned_only is False

    def test_max_too_small(self):
        with pytest.raises(ValidationError):
            CustomerBackfillRequest(max_accounts=0)

    def test_max_too_large(self):
        with pytest.raises(ValidationError):
            CustomerBackfillRequest(max_accounts=501)


class TestCreditUsageItem:
    def test_construction(self):
        obj = CreditUsageItem(
            provider="hunter.io",
            month="2026-01",
            used=45,
            limit=100,
            remaining=55,
        )
        assert obj.remaining == 55


class TestCustomerEnrichmentResult:
    def test_defaults(self):
        obj = CustomerEnrichmentResult()
        assert obj.ok is False
        assert obj.contacts_added == 0
        assert obj.sources_used == []

    def test_success(self):
        obj = CustomerEnrichmentResult(
            ok=True,
            company_id=42,
            contacts_added=3,
            contacts_verified=2,
            sources_used=["hunter.io", "clearbit"],
            status="enriched",
        )
        assert obj.ok is True
        assert obj.company_id == 42


class TestBatchEnrichRequest:
    def test_valid(self):
        obj = BatchEnrichRequest(company_ids=[1, 2, 3])
        assert len(obj.company_ids) == 3

    def test_empty_rejected(self):
        with pytest.raises(ValidationError):
            BatchEnrichRequest(company_ids=[])

    def test_too_many_rejected(self):
        with pytest.raises(ValidationError):
            BatchEnrichRequest(company_ids=list(range(51)))

    def test_force_default_false(self):
        obj = BatchEnrichRequest(company_ids=[1])
        assert obj.force is False


class TestContactSummary:
    def test_all_none_ok(self):
        obj = ContactSummary()
        assert obj.full_name is None
        assert obj.email_verified is False
        assert obj.phone_verified is False

    def test_with_data(self):
        obj = ContactSummary(
            full_name="Jane Smith",
            title="VP Procurement",
            email="jane@acme.com",
            email_verified=True,
            phone="+1-555-0200",
        )
        assert obj.full_name == "Jane Smith"
        assert obj.email_verified is True


class TestEnrichmentStatusResponse:
    def test_minimal(self):
        obj = EnrichmentStatusResponse(company_id=1)
        assert obj.company_id == 1
        assert obj.contacts == []
        assert obj.gaps == []

    def test_with_contacts(self):
        contacts = [ContactSummary(full_name="John", email="john@co.com")]
        obj = EnrichmentStatusResponse(
            company_id=5,
            company_name="Acme",
            enrichment_status="enriched",
            contacts=contacts,
            contact_count=1,
        )
        assert obj.company_name == "Acme"
        assert obj.contact_count == 1
