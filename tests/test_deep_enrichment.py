"""
Tests for the Deep Data Enrichment System.

Covers:
- Signature parsing (regex extraction)
- Specialty detection (brand/commodity matching)
- Confidence routing (three-tier system)
- Enrichment queue API endpoints (list, approve, reject, bulk)
- Enrichment job API endpoints (list, get, cancel)
- On-demand enrichment endpoints
- Stats endpoint

Called by: pytest tests/test_deep_enrichment.py -v
"""

import os
os.environ["TESTING"] = "1"

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    Base, Company, EnrichmentJob, EnrichmentQueue, EmailSignatureExtract,
    User, VendorCard,
)
from app.services.signature_parser import (
    parse_signature_regex,
    cache_signature_extract,
    _extract_signature_block,
)
from app.services.specialty_detector import (
    detect_brands_from_text,
    detect_commodities_from_text,
    BRAND_LIST,
    COMMODITY_MAP,
)
from app.services.deep_enrichment_service import (
    route_enrichment,
    apply_queue_item,
    _apply_field_update,
)


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def admin_client(db_session: Session, admin_user: User) -> TestClient:
    """TestClient with admin auth override for enrichment admin endpoints."""
    from app.database import get_db
    from app.dependencies import require_admin, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_admin():
        return admin_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_admin
    app.dependency_overrides[require_admin] = _override_admin

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def sample_vendor(db_session: Session) -> VendorCard:
    """A vendor card for enrichment tests."""
    card = VendorCard(
        normalized_name="texas instruments",
        display_name="Texas Instruments",
        domain="ti.com",
        emails=["sales@ti.com"],
        phones=["+1-555-0200"],
        sighting_count=100,
        website="https://ti.com",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)
    return card


@pytest.fixture()
def sample_company(db_session: Session) -> Company:
    """A company for enrichment tests."""
    co = Company(
        name="Intel Corporation",
        domain="intel.com",
        website="https://intel.com",
        industry="Semiconductors",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def pending_queue_item(db_session: Session, sample_vendor: VendorCard) -> EnrichmentQueue:
    """A pending enrichment queue item."""
    item = EnrichmentQueue(
        vendor_card_id=sample_vendor.id,
        enrichment_type="company_info",
        field_name="industry",
        current_value=None,
        proposed_value="Semiconductors",
        confidence=0.75,
        source="clearbit",
        status="pending",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item


@pytest.fixture()
def sample_job(db_session: Session, admin_user: User) -> EnrichmentJob:
    """A running enrichment job."""
    job = EnrichmentJob(
        job_type="backfill",
        status="running",
        total_items=50,
        processed_items=10,
        enriched_items=5,
        error_count=1,
        scope={"entity_types": ["vendor", "company"]},
        started_by_id=admin_user.id,
        started_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


# ── Signature Parsing Tests ───────────────────────────────────────────


class TestSignatureParsing:
    """Test regex-based signature extraction."""

    def test_extract_phone(self):
        body = """
Hi, here's our quote.

Best regards,
John Smith
Senior Sales Manager
Arrow Electronics
Phone: +1-555-123-4567
john.smith@arrow.com
"""
        result = parse_signature_regex(body)
        assert result["phone"] is not None
        assert "555" in result["phone"]
        assert result["full_name"] == "John Smith"
        assert result["email"] == "john.smith@arrow.com"

    def test_extract_name_and_title(self):
        body = """
Please see attached.

--
Jane Doe
VP of Procurement
Acme Corp
jane@acme.com
"""
        result = parse_signature_regex(body)
        assert result["full_name"] == "Jane Doe"
        assert "Procurement" in result["title"] or "VP" in result["title"]

    def test_extract_linkedin(self):
        body = """
--
Bob Johnson
linkedin.com/in/bobjohnson
"""
        result = parse_signature_regex(body)
        assert result["linkedin_url"] is not None
        assert "linkedin.com/in/bobjohnson" in result["linkedin_url"]

    def test_extract_website(self):
        body = """
--
Support Team
www.example-corp.com
support@example-corp.com
"""
        result = parse_signature_regex(body)
        assert result["website"] is not None
        assert "example-corp.com" in result["website"]

    def test_empty_body(self):
        result = parse_signature_regex("")
        assert result["confidence"] == 0.0

    def test_no_signature(self):
        body = "Hi, please send me a quote for LM317T. Thanks."
        result = parse_signature_regex(body)
        # Should still try with last 15 lines
        assert isinstance(result["confidence"], float)

    def test_confidence_increases_with_fields(self):
        body_minimal = """
Best,
John
"""
        body_rich = """
Best regards,
John Smith
Director of Sales
Arrow Electronics
Phone: +1-555-123-4567
Mobile: +1-555-987-6543
john.smith@arrow.com
www.arrow.com
linkedin.com/in/johnsmith
"""
        result_minimal = parse_signature_regex(body_minimal)
        result_rich = parse_signature_regex(body_rich)
        assert result_rich["confidence"] > result_minimal["confidence"]

    def test_signature_delimiter_detection(self):
        body = """
Here is the information you requested.

--
Alice Williams
Purchasing Manager
alice@widgets.com
"""
        block = _extract_signature_block(body)
        assert "Alice Williams" in block
        assert "information you requested" not in block

    def test_cache_signature_extract_insert(self, db_session):
        extract = {
            "full_name": "Test User",
            "title": "Engineer",
            "company_name": "TestCo",
            "phone": "+1-555-0000",
            "confidence": 0.8,
            "extraction_method": "regex",
        }
        cache_signature_extract(db_session, "test@testco.com", extract)
        db_session.commit()

        record = (
            db_session.query(EmailSignatureExtract)
            .filter(EmailSignatureExtract.sender_email == "test@testco.com")
            .first()
        )
        assert record is not None
        assert record.full_name == "Test User"
        assert record.title == "Engineer"
        assert record.confidence == 0.8

    def test_cache_signature_extract_upsert_higher_confidence(self, db_session):
        # Insert initial
        cache_signature_extract(db_session, "update@test.com", {
            "full_name": "Old Name",
            "confidence": 0.5,
            "extraction_method": "regex",
        })
        db_session.commit()

        # Upsert with higher confidence
        cache_signature_extract(db_session, "update@test.com", {
            "full_name": "Better Name",
            "title": "CEO",
            "confidence": 0.9,
            "extraction_method": "claude_ai",
        })
        db_session.commit()

        record = (
            db_session.query(EmailSignatureExtract)
            .filter(EmailSignatureExtract.sender_email == "update@test.com")
            .first()
        )
        assert record.full_name == "Better Name"
        assert record.title == "CEO"
        assert record.confidence == 0.9
        assert record.seen_count == 2

    def test_cache_signature_extract_no_overwrite_lower_confidence(self, db_session):
        cache_signature_extract(db_session, "keep@test.com", {
            "full_name": "Good Name",
            "confidence": 0.9,
            "extraction_method": "claude_ai",
        })
        db_session.commit()

        cache_signature_extract(db_session, "keep@test.com", {
            "full_name": "Worse Name",
            "confidence": 0.4,
            "extraction_method": "regex",
        })
        db_session.commit()

        record = (
            db_session.query(EmailSignatureExtract)
            .filter(EmailSignatureExtract.sender_email == "keep@test.com")
            .first()
        )
        assert record.full_name == "Good Name"
        assert record.confidence == 0.9


# ── Specialty Detection Tests ─────────────────────────────────────────


class TestSpecialtyDetection:
    """Test brand and commodity matching."""

    def test_detect_single_brand(self):
        brands = detect_brands_from_text("We have Intel processors in stock")
        assert "Intel" in brands

    def test_detect_multiple_brands(self):
        brands = detect_brands_from_text(
            "We distribute Texas Instruments, NXP, and Microchip products"
        )
        assert "Texas Instruments" in brands
        assert "NXP" in brands
        assert "Microchip" in brands

    def test_no_brands_in_generic_text(self):
        brands = detect_brands_from_text("Please send us your best price")
        assert len(brands) == 0

    def test_empty_text(self):
        assert detect_brands_from_text("") == []
        assert detect_brands_from_text(None) == []

    def test_detect_commodities(self):
        categories = detect_commodities_from_text(
            "DDR4 memory modules and SSD storage drives"
        )
        assert "memory" in categories
        assert "storage" in categories

    def test_detect_capacitors_commodity(self):
        categories = detect_commodities_from_text(
            "MLCC capacitors, tantalum capacitor 100uF"
        )
        assert "capacitors" in categories

    def test_detect_processor_commodity(self):
        categories = detect_commodities_from_text(
            "Xeon E5-2680 server processor"
        )
        assert "processors" in categories

    def test_detect_connectors_commodity(self):
        categories = detect_commodities_from_text(
            "PCIe connectors and backplane headers"
        )
        assert "connectors" in categories

    def test_no_commodities(self):
        categories = detect_commodities_from_text("Hello, how are you?")
        assert len(categories) == 0

    def test_brand_case_insensitive(self):
        brands = detect_brands_from_text("We have NVIDIA gpus and amd EPYC cpus")
        assert "Nvidia" in brands
        assert "AMD" in brands

    def test_brand_list_not_empty(self):
        assert len(BRAND_LIST) > 50

    def test_commodity_map_has_categories(self):
        assert len(COMMODITY_MAP) > 10


# ── Confidence Routing Tests ──────────────────────────────────────────


class TestConfidenceRouting:
    """Test the three-tier confidence routing system."""

    def test_auto_apply_high_confidence(self, db_session, sample_vendor, monkeypatch):
        """Confidence >= 0.8 should auto-apply."""
        from app.config import settings
        monkeypatch.setattr(settings, "deep_enrichment_auto_apply_threshold", 0.8)
        monkeypatch.setattr(settings, "deep_enrichment_review_threshold", 0.5)

        result = route_enrichment(
            db_session,
            entity_type="vendor_card",
            entity_id=sample_vendor.id,
            field_name="industry",
            current_value=None,
            proposed_value="Semiconductors",
            confidence=0.9,
            source="clearbit",
        )
        db_session.commit()

        assert result == "auto_applied"

        # Verify the field was actually updated
        db_session.refresh(sample_vendor)
        assert sample_vendor.industry == "Semiconductors"

        # Verify queue entry was created with auto_applied status
        item = db_session.query(EnrichmentQueue).filter(
            EnrichmentQueue.vendor_card_id == sample_vendor.id,
        ).first()
        assert item.status == "auto_applied"

    def test_pending_medium_confidence(self, db_session, sample_vendor, monkeypatch):
        """Confidence 0.5-0.8 should queue for review."""
        from app.config import settings
        monkeypatch.setattr(settings, "deep_enrichment_auto_apply_threshold", 0.8)
        monkeypatch.setattr(settings, "deep_enrichment_review_threshold", 0.5)

        result = route_enrichment(
            db_session,
            entity_type="vendor_card",
            entity_id=sample_vendor.id,
            field_name="industry",
            current_value=None,
            proposed_value="Electronics",
            confidence=0.65,
            source="hunter",
        )
        db_session.commit()

        assert result == "pending"

        # Verify field was NOT updated
        db_session.refresh(sample_vendor)
        assert sample_vendor.industry is None

        # Queue entry with pending status
        item = db_session.query(EnrichmentQueue).filter(
            EnrichmentQueue.vendor_card_id == sample_vendor.id,
        ).first()
        assert item.status == "pending"

    def test_low_confidence(self, db_session, sample_vendor, monkeypatch):
        """Confidence < 0.5 should create low_confidence record."""
        from app.config import settings
        monkeypatch.setattr(settings, "deep_enrichment_auto_apply_threshold", 0.8)
        monkeypatch.setattr(settings, "deep_enrichment_review_threshold", 0.5)

        result = route_enrichment(
            db_session,
            entity_type="vendor_card",
            entity_id=sample_vendor.id,
            field_name="industry",
            current_value=None,
            proposed_value="Unknown",
            confidence=0.3,
            source="email_signature",
        )
        db_session.commit()

        assert result == "low_confidence"

        item = db_session.query(EnrichmentQueue).filter(
            EnrichmentQueue.vendor_card_id == sample_vendor.id,
        ).first()
        assert item.status == "low_confidence"

    def test_company_routing(self, db_session, sample_company, monkeypatch):
        """Route enrichment to a company entity."""
        from app.config import settings
        monkeypatch.setattr(settings, "deep_enrichment_auto_apply_threshold", 0.8)
        monkeypatch.setattr(settings, "deep_enrichment_review_threshold", 0.5)

        result = route_enrichment(
            db_session,
            entity_type="company",
            entity_id=sample_company.id,
            field_name="employee_size",
            current_value=None,
            proposed_value="10000+",
            confidence=0.85,
            source="clearbit",
        )
        db_session.commit()

        assert result == "auto_applied"
        item = db_session.query(EnrichmentQueue).filter(
            EnrichmentQueue.company_id == sample_company.id,
        ).first()
        assert item is not None
        assert item.status == "auto_applied"

    def test_apply_queue_item_success(self, db_session, pending_queue_item, test_user, sample_vendor):
        """apply_queue_item should update the entity and mark as approved."""
        ok = apply_queue_item(db_session, pending_queue_item, user_id=test_user.id)
        db_session.commit()

        assert ok is True
        assert pending_queue_item.status == "approved"
        assert pending_queue_item.reviewed_by_id == test_user.id
        assert pending_queue_item.reviewed_at is not None

        # Verify the field was applied
        db_session.refresh(sample_vendor)
        assert sample_vendor.industry == "Semiconductors"

    def test_apply_queue_item_already_approved(self, db_session, pending_queue_item):
        """Cannot apply an already-approved item."""
        pending_queue_item.status = "approved"
        db_session.commit()

        ok = apply_queue_item(db_session, pending_queue_item)
        assert ok is False

    def test_apply_field_update_vendor(self, db_session, sample_vendor):
        """_apply_field_update should set the field on the entity."""
        _apply_field_update(db_session, "vendor_card", sample_vendor.id, "industry", "Test Industry")
        db_session.commit()
        db_session.refresh(sample_vendor)
        assert sample_vendor.industry == "Test Industry"

    def test_apply_field_update_nonexistent(self, db_session):
        """Updating a nonexistent entity should not crash."""
        _apply_field_update(db_session, "vendor_card", 99999, "industry", "Test")

    def test_route_with_job_id(self, db_session, sample_vendor, sample_job, monkeypatch):
        """Queue entries should link to the batch job."""
        from app.config import settings
        monkeypatch.setattr(settings, "deep_enrichment_auto_apply_threshold", 0.8)
        monkeypatch.setattr(settings, "deep_enrichment_review_threshold", 0.5)

        route_enrichment(
            db_session,
            entity_type="vendor_card",
            entity_id=sample_vendor.id,
            field_name="hq_city",
            current_value=None,
            proposed_value="Dallas",
            confidence=0.6,
            source="clearbit",
            job_id=sample_job.id,
        )
        db_session.commit()

        item = db_session.query(EnrichmentQueue).filter(
            EnrichmentQueue.field_name == "hq_city",
        ).first()
        assert item.batch_job_id == sample_job.id


# ── Enrichment Queue API Tests ────────────────────────────────────────


class TestEnrichmentQueueAPI:
    """Test enrichment queue REST endpoints."""

    def test_list_queue_empty(self, client):
        resp = client.get("/api/enrichment/queue")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_queue_with_items(self, client, db_session, sample_vendor):
        # Create items
        for i in range(3):
            item = EnrichmentQueue(
                vendor_card_id=sample_vendor.id,
                enrichment_type="company_info",
                field_name=f"field_{i}",
                proposed_value=f"value_{i}",
                confidence=0.7,
                source="clearbit",
                status="pending",
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(item)
        db_session.commit()

        resp = client.get("/api/enrichment/queue")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert len(data["items"]) == 3

    def test_list_queue_filter_status(self, client, db_session, sample_vendor):
        for status in ("pending", "approved", "rejected"):
            item = EnrichmentQueue(
                vendor_card_id=sample_vendor.id,
                enrichment_type="company_info",
                field_name=f"field_{status}",
                proposed_value="val",
                confidence=0.7,
                source="test",
                status=status,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(item)
        db_session.commit()

        resp = client.get("/api/enrichment/queue?status=pending")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

        resp = client.get("/api/enrichment/queue?status=all")
        assert resp.status_code == 200
        assert resp.json()["total"] == 3

    def test_list_queue_filter_entity_type(self, client, db_session, sample_vendor, sample_company):
        db_session.add(EnrichmentQueue(
            vendor_card_id=sample_vendor.id,
            enrichment_type="company_info",
            field_name="f1",
            proposed_value="v1",
            confidence=0.7,
            source="test",
            status="pending",
        ))
        db_session.add(EnrichmentQueue(
            company_id=sample_company.id,
            enrichment_type="company_info",
            field_name="f2",
            proposed_value="v2",
            confidence=0.7,
            source="test",
            status="pending",
        ))
        db_session.commit()

        resp = client.get("/api/enrichment/queue?status=pending&entity_type=vendor")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1
        assert resp.json()["items"][0]["entity_type"] == "vendor"

    def test_approve_item(self, client, db_session, pending_queue_item):
        resp = client.post(f"/api/enrichment/queue/{pending_queue_item.id}/approve")
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

        db_session.refresh(pending_queue_item)
        assert pending_queue_item.status == "approved"

    def test_approve_nonexistent(self, client):
        resp = client.post("/api/enrichment/queue/99999/approve")
        assert resp.status_code == 404

    def test_approve_already_approved(self, client, db_session, pending_queue_item):
        pending_queue_item.status = "approved"
        db_session.commit()

        resp = client.post(f"/api/enrichment/queue/{pending_queue_item.id}/approve")
        assert resp.status_code == 400

    def test_reject_item(self, client, db_session, pending_queue_item):
        resp = client.post(f"/api/enrichment/queue/{pending_queue_item.id}/reject")
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"

        db_session.refresh(pending_queue_item)
        assert pending_queue_item.status == "rejected"

    def test_reject_nonexistent(self, client):
        resp = client.post("/api/enrichment/queue/99999/reject")
        assert resp.status_code == 404

    def test_reject_already_rejected(self, client, db_session, pending_queue_item):
        pending_queue_item.status = "rejected"
        db_session.commit()

        resp = client.post(f"/api/enrichment/queue/{pending_queue_item.id}/reject")
        assert resp.status_code == 400

    def test_bulk_approve(self, client, db_session, sample_vendor):
        ids = []
        for i in range(3):
            item = EnrichmentQueue(
                vendor_card_id=sample_vendor.id,
                enrichment_type="company_info",
                field_name=f"bulk_{i}",
                proposed_value=f"val_{i}",
                confidence=0.7,
                source="test",
                status="pending",
            )
            db_session.add(item)
            db_session.flush()
            ids.append(item.id)
        db_session.commit()

        resp = client.post("/api/enrichment/queue/bulk-approve", json={"ids": ids})
        assert resp.status_code == 200
        data = resp.json()
        assert data["approved"] == 3
        assert data["failed"] == 0


# ── Enrichment Job API Tests ──────────────────────────────────────────


class TestEnrichmentJobAPI:
    """Test enrichment job REST endpoints."""

    def test_list_jobs_empty(self, client):
        resp = client.get("/api/enrichment/jobs")
        assert resp.status_code == 200
        assert resp.json()["jobs"] == []

    def test_list_jobs(self, client, sample_job):
        resp = client.get("/api/enrichment/jobs")
        assert resp.status_code == 200
        jobs = resp.json()["jobs"]
        assert len(jobs) == 1
        assert jobs[0]["id"] == sample_job.id
        assert jobs[0]["status"] == "running"

    def test_get_job(self, client, sample_job):
        resp = client.get(f"/api/enrichment/jobs/{sample_job.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == sample_job.id
        assert data["status"] == "running"
        assert data["total_items"] == 50
        assert data["processed_items"] == 10
        assert data["progress_pct"] == 20.0

    def test_get_job_not_found(self, client):
        resp = client.get("/api/enrichment/jobs/99999")
        assert resp.status_code == 404

    def test_cancel_job(self, admin_client, sample_job):
        resp = admin_client.post(f"/api/enrichment/jobs/{sample_job.id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

    def test_cancel_completed_job(self, admin_client, db_session, sample_job):
        sample_job.status = "completed"
        db_session.commit()

        resp = admin_client.post(f"/api/enrichment/jobs/{sample_job.id}/cancel")
        assert resp.status_code == 400


# ── Stats API Tests ───────────────────────────────────────────────────


class TestEnrichmentStatsAPI:
    """Test enrichment stats endpoint."""

    def test_stats_empty(self, client):
        resp = client.get("/api/enrichment/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["queue_pending"] == 0
        assert data["active_jobs"] == 0

    def test_stats_with_data(self, client, db_session, sample_vendor, sample_job):
        # Add some queue items
        for status in ("pending", "pending", "approved", "rejected"):
            db_session.add(EnrichmentQueue(
                vendor_card_id=sample_vendor.id,
                enrichment_type="company_info",
                field_name=f"f_{status}",
                proposed_value="v",
                confidence=0.7,
                source="test",
                status=status,
            ))
        db_session.commit()

        resp = client.get("/api/enrichment/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["queue_pending"] == 2
        assert data["queue_approved"] == 1
        assert data["queue_rejected"] == 1
        assert data["active_jobs"] == 1  # sample_job is "running"

    def test_stats_vendor_enrichment_coverage(self, client, db_session, sample_vendor):
        # sample_vendor has no deep_enrichment_at
        resp = client.get("/api/enrichment/stats")
        data = resp.json()
        assert data["vendors_total"] >= 1
        assert data["vendors_enriched"] == 0

        # Mark vendor as enriched
        sample_vendor.deep_enrichment_at = datetime.now(timezone.utc)
        db_session.commit()

        resp = client.get("/api/enrichment/stats")
        data = resp.json()
        assert data["vendors_enriched"] >= 1


# ── Queue Item Shape Tests ────────────────────────────────────────────


class TestQueueItemShape:
    """Verify the shape of queue items returned by API."""

    def test_queue_item_has_expected_fields(self, client, db_session, sample_vendor):
        db_session.add(EnrichmentQueue(
            vendor_card_id=sample_vendor.id,
            enrichment_type="brand_tags",
            field_name="brand_tags",
            proposed_value=json.dumps(["Intel", "AMD"]),
            confidence=0.85,
            source="specialty_analysis",
            status="pending",
            created_at=datetime.now(timezone.utc),
        ))
        db_session.commit()

        resp = client.get("/api/enrichment/queue")
        item = resp.json()["items"][0]

        expected_keys = {
            "id", "entity_type", "entity_name", "enrichment_type",
            "field_name", "current_value", "proposed_value",
            "confidence", "source", "status", "created_at",
        }
        assert expected_keys.issubset(set(item.keys()))
        assert item["entity_type"] == "vendor"
        assert item["entity_name"] == "Texas Instruments"
        assert item["enrichment_type"] == "brand_tags"
        assert item["confidence"] == 0.85
