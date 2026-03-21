"""Tests for NC Phase 2: Config, MPN Normalizer, Queue Manager.

Called by: pytest
Depends on: conftest.py, nc_worker modules
"""

from datetime import datetime, timezone
from unittest.mock import patch

from app.models import NcSearchQueue, Requirement
from app.services.nc_worker.config import NcConfig
from app.services.nc_worker.mpn_normalizer import strip_packaging_suffixes as normalize_mpn
from app.services.nc_worker.queue_manager import (
    enqueue_for_nc_search,
    get_next_queued_item,
    get_queue_stats,
    mark_completed,
    mark_status,
)

# ── Config Tests ─────────────────────────────────────────────────────


def test_config_defaults():
    """NcConfig loads sensible defaults when no env vars set."""
    cfg = NcConfig()
    assert cfg.NC_MAX_DAILY_SEARCHES == 75
    assert cfg.NC_MAX_HOURLY_SEARCHES == 12
    assert cfg.NC_MIN_DELAY_SECONDS == 120
    assert cfg.NC_MAX_DELAY_SECONDS == 420
    assert cfg.NC_TYPICAL_DELAY_SECONDS == 240
    assert cfg.NC_DEDUP_WINDOW_DAYS == 7
    assert cfg.NC_BUSINESS_HOURS_START == 8
    assert cfg.NC_BUSINESS_HOURS_END == 18


def test_config_from_env():
    """NcConfig reads overrides from environment variables."""
    with patch.dict("os.environ", {"NC_MAX_DAILY_SEARCHES": "50", "NC_USERNAME": "test@co.com"}):
        cfg = NcConfig()
        assert cfg.NC_MAX_DAILY_SEARCHES == 50
        assert cfg.NC_USERNAME == "test@co.com"


# ── MPN Normalizer Tests ────────────────────────────────────────────


def test_normalize_uppercase_and_strip():
    """Uppercase and strip whitespace."""
    assert normalize_mpn("  p5040nsn72qc  ") == "P5040NSN72QC"


def test_normalize_no_change_package_code():
    """DR is a package code (SOIC), not a suffix — should NOT be stripped."""
    assert normalize_mpn("LM358DR") == "LM358DR"


def test_normalize_strip_tape_reel_dash():
    """-TR (tape and reel) is stripped."""
    assert normalize_mpn("STM32F103C8T6-TR") == "STM32F103C8T6"


def test_normalize_strip_tape_reel_slash():
    """/TR (tape and reel) is stripped."""
    assert normalize_mpn("AD8232ACPZ/TR") == "AD8232ACPZ"


def test_normalize_strip_cut_tape():
    """/CT (cut tape) is stripped."""
    assert normalize_mpn("SN74HC595N/CT") == "SN74HC595N"


def test_normalize_strip_no_datasheet():
    """-ND (no datasheet, DigiKey) is stripped."""
    assert normalize_mpn("LM7805CT-ND") == "LM7805CT"


def test_normalize_strip_digikey_reel():
    """-DKR (DigiKey reel) is stripped."""
    assert normalize_mpn("RC0805FR-071KL-DKR") == "RC0805FR-071KL"


def test_normalize_strip_lead_free():
    """#PBF (lead-free) is stripped."""
    assert normalize_mpn("IRF540N#PBF") == "IRF540N"


def test_normalize_strip_nopb():
    """/NOPB (no lead, TI) is stripped."""
    assert normalize_mpn("LM317T/NOPB") == "LM317T"


def test_normalize_strip_reel_suffix():
    """-RL (reel packaging) is stripped. The base part is the same component."""
    assert normalize_mpn("ADP3338AKCZ-3.3-RL") == "ADP3338AKCZ-3.3"


def test_normalize_strip_internal_whitespace():
    """Internal whitespace is removed."""
    assert normalize_mpn("STM 32F 103") == "STM32F103"


def test_normalize_empty():
    """Empty and None return empty string."""
    assert normalize_mpn("") == ""
    assert normalize_mpn("   ") == ""


def test_normalize_pbf_dash():
    """-PBF (lead-free, dash form) is stripped."""
    assert normalize_mpn("LT1963EST-3.3-PBF") == "LT1963EST-3.3"


# ── Queue Manager Tests ─────────────────────────────────────────────


def test_enqueue_creates_item(db_session, test_requisition):
    """enqueue_for_nc_search creates a pending queue item."""
    req = test_requisition.requirements[0]
    item = enqueue_for_nc_search(req.id, db_session)

    assert item is not None
    assert item.mpn == "LM317T"
    assert item.normalized_mpn == "LM317T"
    assert item.status == "pending"
    assert item.requirement_id == req.id
    assert item.requisition_id == test_requisition.id


def test_enqueue_dedup_returns_none(db_session, test_requisition, test_user):
    """When a recent completed search exists for same MPN, enqueue returns None."""
    req = test_requisition.requirements[0]

    # Simulate a completed search from recently
    existing = NcSearchQueue(
        requirement_id=req.id,
        requisition_id=test_requisition.id,
        mpn="LM317T",
        normalized_mpn="LM317T",
        status="completed",
        last_searched_at=datetime.now(timezone.utc),
    )
    db_session.add(existing)
    db_session.commit()

    # Create a second requisition with same MPN
    from app.models import Requisition

    req2 = Requisition(
        name="REQ-TEST-002",
        customer_name="Other Co",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req2)
    db_session.flush()
    req_item2 = Requirement(
        requisition_id=req2.id,
        primary_mpn="LM317T",
        target_qty=500,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req_item2)
    db_session.commit()

    result = enqueue_for_nc_search(req_item2.id, db_session)
    assert result is None  # Deduped


def test_enqueue_no_mpn_returns_none(db_session, test_user):
    """Requirements without an MPN are skipped."""
    from app.models import Requisition

    req = Requisition(
        name="REQ-NOMPN",
        customer_name="Test Co",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn=None,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()

    result = enqueue_for_nc_search(item.id, db_session)
    assert result is None


def test_enqueue_already_queued_returns_existing(db_session, test_requisition):
    """If requirement is already queued, returns existing item without creating
    duplicate."""
    req = test_requisition.requirements[0]
    item1 = enqueue_for_nc_search(req.id, db_session)
    item2 = enqueue_for_nc_search(req.id, db_session)
    assert item1.id == item2.id


def test_get_next_queued_item_ordering(db_session, test_user):
    """get_next_queued_item returns by priority ASC, then created_at ASC."""
    from app.models import Requisition

    items = []
    for i, (mpn, priority) in enumerate([("PARTC", 3), ("PARTA", 1), ("PARTB", 2)]):
        req = Requisition(
            name=f"REQ-{i}",
            customer_name="Test",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn=mpn,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(r)
        db_session.flush()
        q = NcSearchQueue(
            requirement_id=r.id,
            requisition_id=req.id,
            mpn=mpn,
            normalized_mpn=mpn,
            status="queued",
            priority=priority,
        )
        db_session.add(q)
        items.append(q)
    db_session.commit()

    next_item = get_next_queued_item(db_session)
    assert next_item.mpn == "PARTA"  # priority 1 comes first


def test_get_next_queued_item_empty(db_session):
    """Returns None when no queued items exist."""
    assert get_next_queued_item(db_session) is None


def test_mark_status(db_session, test_requisition):
    """mark_status updates status and updated_at."""
    req = test_requisition.requirements[0]
    item = NcSearchQueue(
        requirement_id=req.id,
        requisition_id=test_requisition.id,
        mpn="TEST",
        normalized_mpn="TEST",
        status="pending",
    )
    db_session.add(item)
    db_session.commit()

    mark_status(db_session, item, "queued")
    assert item.status == "queued"

    mark_status(db_session, item, "failed", error="Connection timeout")
    assert item.status == "failed"
    assert item.error_message == "Connection timeout"


def test_mark_completed(db_session, test_requisition):
    """mark_completed sets status, timestamps, and result counts."""
    req = test_requisition.requirements[0]
    item = NcSearchQueue(
        requirement_id=req.id,
        requisition_id=test_requisition.id,
        mpn="TEST",
        normalized_mpn="TEST",
        status="searching",
        search_count=0,
    )
    db_session.add(item)
    db_session.commit()

    mark_completed(db_session, item, results_found=25, sightings_created=18)
    assert item.status == "completed"
    assert item.results_count == 25
    assert item.search_count == 1
    assert item.last_searched_at is not None


def test_get_queue_stats(db_session, test_user):
    """get_queue_stats returns counts by status."""
    from app.models import Requisition

    for i, status in enumerate(["pending", "pending", "queued", "completed", "failed"]):
        req = Requisition(
            name=f"REQ-STAT-{i}",
            customer_name="Test",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn=f"PART{i}",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(r)
        db_session.flush()
        q = NcSearchQueue(
            requirement_id=r.id,
            requisition_id=req.id,
            mpn=f"PART{i}",
            normalized_mpn=f"PART{i}",
            status=status,
            last_searched_at=datetime.now(timezone.utc) if status == "completed" else None,
        )
        db_session.add(q)
    db_session.commit()

    stats = get_queue_stats(db_session)
    assert stats["pending"] == 2
    assert stats["queued"] == 1
    assert stats["completed"] == 1
    assert stats["failed"] == 1
    assert stats["remaining"] == 3  # 2 pending + 1 queued
    assert stats["total_today"] == 1
