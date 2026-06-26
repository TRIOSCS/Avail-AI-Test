"""test_datasheet_triggers.py — verify capture_datasheet is enqueued at each trigger
site.

Covers:
  1. dossier_hero (GET /v2/partials/search/dossier/hero) — search trigger
  2. quick_source_rfq (POST /v2/partials/search/quick-source/rfq) — RFQ trigger
  3. quick_source_offer (POST /v2/partials/search/quick-source/offer) — offer trigger
  4. add_requirements (POST /api/requisitions/{req_id}/requirements) — req-add trigger

CRITICAL: patch BOTH safe_background_task (to stop it from scheduling) AND
capture_datasheet (so no real coroutine is created, preventing "coroutine was never
awaited" RuntimeWarning). Then assert triggers fired.
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.intelligence import MaterialCard
from app.models.sourcing import Requisition

# ── helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def card(db_session):
    """A live MaterialCard for LM317T."""
    c = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T")
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture()
def scratch_req(db_session, test_user):
    """A requisition owned by the test user."""
    r = Requisition(
        name="Trigger Test Req",
        created_by=test_user.id,
        status="open",
    )
    db_session.add(r)
    db_session.commit()
    db_session.refresh(r)
    return r


# ── 1. search trigger ─────────────────────────────────────────────────────────


def test_search_enqueues_capture(client, db_session, card):
    """dossier_hero enqueues capture_datasheet when an MPN is supplied."""
    with (
        patch("app.routers.part_dossier.safe_background_task", new_callable=AsyncMock) as bg,
        patch("app.services.datasheet_capture.capture_datasheet", new_callable=MagicMock) as _cd,
    ):
        resp = client.get("/v2/partials/search/dossier/hero", params={"mpn": "LM317T"})
    assert resp.status_code == 200
    assert bg.called, "safe_background_task was not called from dossier_hero"


def test_search_no_mpn_skips_capture(client, db_session):
    """dossier_hero with blank MPN should NOT enqueue (nothing to capture)."""
    with (
        patch("app.routers.part_dossier.safe_background_task", new_callable=AsyncMock) as bg,
        patch("app.services.datasheet_capture.capture_datasheet", new_callable=MagicMock),
    ):
        resp = client.get("/v2/partials/search/dossier/hero", params={"mpn": ""})
    assert resp.status_code == 200
    assert not bg.called, "safe_background_task should NOT be called for blank MPN"


# ── 2. RFQ trigger ────────────────────────────────────────────────────────────


def test_rfq_enqueues_capture(client, db_session, card):
    """quick_source_rfq enqueues capture_datasheet when mpn is supplied."""
    with (
        patch("app.routers.part_dossier.safe_background_task", new_callable=AsyncMock) as bg,
        patch("app.services.datasheet_capture.capture_datasheet", new_callable=MagicMock),
    ):
        resp = client.post(
            "/v2/partials/search/quick-source/rfq",
            data={"mpn": "LM317T", "items": "", "vendor_name": ""},
        )
    # Response is HX-Redirect (200) or 400 (no mpn guard) — we only care bg was called
    assert bg.called, "safe_background_task was not called from quick_source_rfq"


# ── 3. Offer trigger ──────────────────────────────────────────────────────────


def test_offer_enqueues_capture(client, db_session, card):
    """quick_source_offer enqueues capture_datasheet when mpn is supplied."""
    with (
        patch("app.routers.part_dossier.safe_background_task", new_callable=AsyncMock) as bg,
        patch("app.services.datasheet_capture.capture_datasheet", new_callable=MagicMock),
    ):
        resp = client.post(
            "/v2/partials/search/quick-source/offer",
            data={"mpn": "LM317T", "items": "", "vendor_name": ""},
        )
    assert bg.called, "safe_background_task was not called from quick_source_offer"


# ── 4. Requirement-add trigger ────────────────────────────────────────────────


def test_add_requirements_enqueues_capture(client, db_session, scratch_req):
    """add_requirements enqueues capture_datasheet for each distinct MPN created."""
    with (
        patch("app.routers.requisitions.requirements.safe_background_task", new_callable=AsyncMock) as bg,
        patch("app.services.datasheet_capture.capture_datasheet", new_callable=MagicMock),
    ):
        resp = client.post(
            f"/api/requisitions/{scratch_req.id}/requirements",
            json={"primary_mpn": "LM317T", "manufacturer": "Texas Instruments", "target_qty": 10},
        )
    assert resp.status_code == 200
    assert bg.called, "safe_background_task was not called from add_requirements"
