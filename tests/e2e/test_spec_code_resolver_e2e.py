"""End-to-end happy path for the IBM spec-code resolver.

Creates a requisition with ``primary_mpn="SPREJ"``, stubs the LLM client and
the connector fanout, runs ``search_requirement()`` and asserts:
- at least one sighting is persisted with ``resolved_via_spec_code="SPREJ"``
  and ``source_mpn`` matching the resolved AVL MPN;
- an ``OemSpecCodePending`` row exists with the requirement id appended to
  ``used_in_requirement_ids``.

This file lives under ``tests/e2e/`` per the spec's testing layout. The
project's ``pytest.ini`` excludes ``tests/e2e/`` from the default test run
because the directory historically held Playwright tests; this one uses the
shared in-memory SQLite fixtures (via ``tests/conftest.py``) and runs in
seconds when invoked explicitly:

    env TESTING=1 PYTHONPATH=/root/availai-worktrees/spec-ibm-resolver \
        pytest tests/e2e/test_spec_code_resolver_e2e.py -v --override-ini="addopts="
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# Make sure the top-level ``tests/`` conftest (with the SQLite engine and
# ``db_session`` fixture) is on sys.path even though this file lives one
# directory down.
_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

# Honour the project's TESTING flag so app imports skip real API setup.
os.environ.setdefault("TESTING", "1")

from app import search_service  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import Requirement, User  # noqa: E402
from app.models.sourcing import (  # noqa: E402
    OemSpecCodePending,
    Requisition,
    Sighting,
)


@pytest.fixture
def enable_flag(monkeypatch):
    monkeypatch.setattr(settings, "spec_resolver_enabled", True)


@pytest.fixture
def user(db_session):
    u = User(
        email="e2e@example.com",
        name="E2E User",
        role="admin",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(u)
    db_session.commit()
    return u


async def test_e2e_sprej_resolution_persists_sighting_and_pending(db_session, enable_flag, user, monkeypatch):
    rset = Requisition(
        name="e2e-sprej",
        customer_name="Acme",
        status="active",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(rset)
    db_session.flush()
    req = Requirement(
        requisition_id=rset.id,
        primary_mpn="SPREJ",
        target_qty=700,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()
    req_id = req.id

    async def fake_fetch_fresh(mpns, db):
        if list(mpns) == ["SPREJ"]:
            return ([], [{"source": "mouser", "status": "ok", "results": 0, "ms": 5, "error": None}])
        return (
            [
                {
                    "vendor_name": "OEMSecrets-Broker",
                    "mpn_matched": mpns[0],
                    "mpn": mpns[0],
                    "manufacturer": "Murata",
                    "qty_available": 1500,
                    "unit_price": 0.42,
                    "currency": "USD",
                    "source_type": "oemsecrets",
                    "is_authorized": False,
                    "confidence": 3,
                }
            ],
            [{"source": "oemsecrets", "status": "ok", "results": 1, "ms": 10, "error": None}],
        )

    async def fake_claude(**kwargs):
        return {
            "avl": [
                {
                    "mpn": "GRM188R71H103KA01D",
                    "manufacturer": "Murata",
                    "rank": 1,
                    "notes": "primary",
                }
            ],
            "confidence": 0.9,
            "citations": [{"url": "https://www.ibm.com/redbook", "snippet": "SPREJ..."}],
            "reasoning": "matched IBM redbook",
        }

    monkeypatch.setattr(search_service, "_fetch_fresh", fake_fetch_fresh)
    with patch("app.services.spec_code_resolver._default_claude_call", new=fake_claude):
        await search_service.search_requirement(req, db_session)

    # Sighting persisted with spec-code lineage.
    db_session.expire_all()
    sightings = db_session.query(Sighting).filter_by(requirement_id=req_id).all()
    assert len(sightings) >= 1
    assert any(s.resolved_via_spec_code == "SPREJ" for s in sightings)
    assert any(s.source_mpn == "GRM188R71H103KA01D" for s in sightings)

    # Pending row exists with the requirement id appended.
    pending = db_session.query(OemSpecCodePending).filter_by(oem="IBM", spec_code="SPREJ").one()
    assert pending.proposed_avl[0]["mpn"] == "GRM188R71H103KA01D"
    assert req_id in (pending.used_in_requirement_ids or [])
