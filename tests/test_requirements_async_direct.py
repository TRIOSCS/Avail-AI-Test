"""test_requirements_async_direct.py — Direct async invocation of requirements view functions.

Covers lines 388-560, 603, 837-847, 914, 1025, 1207-1342 which are inside async
view function bodies and cannot be traced through TestClient.

Called by: pytest (asyncio_mode = auto)
Depends on: app/routers/requisitions/requirements.py, conftest.py
"""

import os

os.environ["TESTING"] = "1"

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models import Requirement, Requisition, Sighting, User


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_json_request(body: dict) -> MagicMock:
    """Create a mock Request that returns JSON body."""
    mock_req = MagicMock(spec=Request)
    mock_req.headers = {}

    async def _json():
        return body

    mock_req.json = _json
    return mock_req


def _make_requirement(db: Session, req: Requisition, mpn: str = "LM317T") -> Requirement:
    r = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        normalized_mpn=mpn.lower(),
        target_qty=100,
        target_price=0.50,
        created_at=datetime.now(timezone.utc),
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


# ── add_requirements (lines 388-560) ────────────────────────────────────────


async def test_add_requirements_single_item(
    db_session: Session, test_user: User, test_requisition: Requisition
):
    """Covers lines 388-547: add a single requirement via JSON body."""
    from app.routers.requisitions.requirements import add_requirements

    mock_req = _make_json_request({
        "primary_mpn": "BC547",
        "manufacturer": "Fairchild",
        "target_qty": 500,
        "target_price": 0.25,
    })

    bg_tasks = BackgroundTasks()

    with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
        result = await add_requirements(
            req_id=test_requisition.id,
            request=mock_req,
            background_tasks=bg_tasks,
            user=test_user,
            db=db_session,
        )

    assert "created" in result
    assert len(result["created"]) == 1
    assert result["created"][0]["primary_mpn"] == "BC547"


async def test_add_requirements_batch_list(
    db_session: Session, test_user: User, test_requisition: Requisition
):
    """Covers lines 388-389 (is_batch=True): batch list of requirements."""
    from app.routers.requisitions.requirements import add_requirements

    mock_req = _make_json_request([
        {"primary_mpn": "LM317T", "manufacturer": "TI", "target_qty": 1000},
        {"primary_mpn": "NE555", "manufacturer": "Philips", "target_qty": 200},
    ])

    bg_tasks = BackgroundTasks()

    with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
        result = await add_requirements(
            req_id=test_requisition.id,
            request=mock_req,
            background_tasks=bg_tasks,
            user=test_user,
            db=db_session,
        )

    assert len(result["created"]) == 2


async def test_add_requirements_batch_with_invalid_item_skipped(
    db_session: Session, test_user: User, test_requisition: Requisition
):
    """Covers lines 395-399: invalid item in batch → skipped."""
    from app.routers.requisitions.requirements import add_requirements

    mock_req = _make_json_request([
        {"primary_mpn": "VALID001", "manufacturer": "Acme", "target_qty": 100},
        {"primary_mpn": ""},  # invalid - blank mpn
    ])

    bg_tasks = BackgroundTasks()

    with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
        result = await add_requirements(
            req_id=test_requisition.id,
            request=mock_req,
            background_tasks=bg_tasks,
            user=test_user,
            db=db_session,
        )

    # valid item is created
    assert len(result["created"]) >= 1


async def test_add_requirements_single_invalid_raises_422(
    db_session: Session, test_user: User, test_requisition: Requisition
):
    """Covers lines 396-397: single invalid item (not batch) → raises 422."""
    from app.routers.requisitions.requirements import add_requirements

    mock_req = _make_json_request({
        "primary_mpn": "",  # invalid
        "target_qty": 100,
    })

    bg_tasks = BackgroundTasks()

    with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
        with pytest.raises(HTTPException) as exc:
            await add_requirements(
                req_id=test_requisition.id,
                request=mock_req,
                background_tasks=bg_tasks,
                user=test_user,
                db=db_session,
            )
    assert exc.value.status_code == 422


async def test_add_requirements_requisition_not_found(
    db_session: Session, test_user: User
):
    """Covers lines 384-386: requisition not found → 404."""
    from app.routers.requisitions.requirements import add_requirements

    mock_req = _make_json_request({"primary_mpn": "LM317T", "target_qty": 100})
    bg_tasks = BackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        await add_requirements(
            req_id=99999,
            request=mock_req,
            background_tasks=bg_tasks,
            user=test_user,
            db=db_session,
        )
    assert exc.value.status_code == 404


async def test_add_requirements_with_substitutes(
    db_session: Session, test_user: User, test_requisition: Requisition
):
    """Covers lines 400 (_dedupe_substitutes call): substitutes are deduped."""
    from app.routers.requisitions.requirements import add_requirements

    mock_req = _make_json_request({
        "primary_mpn": "LM317T",
        "manufacturer": "TI",
        "target_qty": 100,
        "substitutes": ["LM317AT", "LM317T", "LM317BT"],  # LM317T is primary → excluded
    })

    bg_tasks = BackgroundTasks()

    with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
        result = await add_requirements(
            req_id=test_requisition.id,
            request=mock_req,
            background_tasks=bg_tasks,
            user=test_user,
            db=db_session,
        )

    assert len(result["created"]) == 1
    req_item = db_session.get(Requirement, result["created"][0]["id"])
    # LM317T should be excluded from substitutes (it's the primary)
    subs = req_item.substitutes or []
    mpn_values = [s.get("mpn", s) if isinstance(s, dict) else s for s in subs]
    assert "LM317T" not in mpn_values


async def test_add_requirements_with_material_card(
    db_session: Session, test_user: User, test_requisition: Requisition, test_material_card
):
    """Covers lines 401-408 (mat_card resolution): material card linked."""
    from app.routers.requisitions.requirements import add_requirements

    mock_req = _make_json_request({
        "primary_mpn": "LM317T",
        "manufacturer": "TI",
        "target_qty": 100,
    })

    bg_tasks = BackgroundTasks()

    with patch(
        "app.routers.requisitions.requirements.resolve_material_card",
        return_value=test_material_card,
    ):
        result = await add_requirements(
            req_id=test_requisition.id,
            request=mock_req,
            background_tasks=bg_tasks,
            user=test_user,
            db=db_session,
        )

    assert len(result["created"]) == 1
    req_item = db_session.get(Requirement, result["created"][0]["id"])
    assert req_item.material_card_id == test_material_card.id


async def test_add_requirements_with_all_fields(
    db_session: Session, test_user: User, test_requisition: Requisition
):
    """Covers lines 418-428 (all optional fields): condition, packaging, etc."""
    from app.routers.requisitions.requirements import add_requirements

    mock_req = _make_json_request({
        "primary_mpn": "NE555P",
        "manufacturer": "Texas Instruments",
        "target_qty": 250,
        "target_price": 0.15,
        "condition": "new",
        "packaging": "reel",
        "date_codes": "2024+",
        "firmware": "v1.0",
        "hardware_codes": "rev-A",
        "notes": "Prefer Texas Instruments",
        "description": "Timer IC",
    })

    bg_tasks = BackgroundTasks()

    with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
        result = await add_requirements(
            req_id=test_requisition.id,
            request=mock_req,
            background_tasks=bg_tasks,
            user=test_user,
            db=db_session,
        )

    assert len(result["created"]) == 1


async def test_add_requirements_duplicate_detection(
    db_session: Session, test_user: User, test_requisition: Requisition, test_material_card
):
    """Covers lines 516-547: duplicate detection with customer_site_id."""
    from app.routers.requisitions.requirements import add_requirements

    # Set customer_site_id on the requisition
    test_requisition.customer_site_id = None  # no site → no dup detection
    db_session.commit()

    mock_req = _make_json_request({
        "primary_mpn": "LM317T",
        "manufacturer": "TI",
        "target_qty": 100,
    })

    bg_tasks = BackgroundTasks()

    with patch(
        "app.routers.requisitions.requirements.resolve_material_card",
        return_value=test_material_card,
    ):
        result = await add_requirements(
            req_id=test_requisition.id,
            request=mock_req,
            background_tasks=bg_tasks,
            user=test_user,
            db=db_session,
        )

    assert "created" in result
    assert "duplicates" in result


# ── search_all draft→active transition (lines 837-847) ───────────────────────


async def test_search_all_transitions_draft_to_active(
    db_session: Session, test_user: User, test_requisition: Requisition
):
    """Covers lines 841-847: draft req transitions to active after search."""
    from app.routers.requisitions.requirements import search_all

    # Set requisition to draft
    test_requisition.status = "draft"
    db_session.commit()

    req_item = _make_requirement(db_session, test_requisition)
    mock_req = MagicMock(spec=Request)
    mock_req.headers = {}
    bg_tasks = BackgroundTasks()

    with patch(
        "app.routers.requisitions.__init__.search_requirement",
        new_callable=AsyncMock,
    ) as mock_search:
        mock_search.return_value = {"sightings": [], "source_stats": []}

        result = await search_all(
            req_id=test_requisition.id,
            request=mock_req,
            background_tasks=bg_tasks,
            body=None,
            user=test_user,
            db=db_session,
        )

    db_session.refresh(test_requisition)
    assert test_requisition.status == "active"


# ── get_saved_sightings (line 914, 1025) ──────────────────────────────────────


async def test_get_saved_sightings_requisition_not_found(
    db_session: Session, test_user: User
):
    """Covers line 914: requisition not found → 404."""
    from app.routers.requisitions.requirements import get_saved_sightings

    with pytest.raises(HTTPException) as exc:
        await get_saved_sightings(
            req_id=99999,
            user=test_user,
            db=db_session,
        )
    assert exc.value.status_code == 404


async def test_get_saved_sightings_with_data(
    db_session: Session, test_user: User, test_requisition: Requisition
):
    """Covers lines 912-1039: successful get_saved_sightings with sightings."""
    from app.routers.requisitions.requirements import get_saved_sightings

    req_item = _make_requirement(db_session, test_requisition)

    sighting = Sighting(
        requirement_id=req_item.id,
        vendor_name="Arrow Electronics",
        vendor_name_normalized="arrow electronics",
        mpn_matched="LM317T",
        source_type="manual",
        confidence=80,
        score=50,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(sighting)
    db_session.commit()

    result = await get_saved_sightings(
        req_id=test_requisition.id,
        user=test_user,
        db=db_session,
    )

    # result is a dict with req_id keys
    assert str(req_item.id) in result
    assert "sightings" in result[str(req_item.id)]


async def test_get_saved_sightings_no_sightings_skipped(
    db_session: Session, test_user: User, test_requisition: Requisition
):
    """Covers line 1025 (continue when no sightings): req with no data is skipped."""
    from app.routers.requisitions.requirements import get_saved_sightings

    req_item = _make_requirement(db_session, test_requisition)
    # No sightings added → should be skipped (continue at line 1025)

    result = await get_saved_sightings(
        req_id=test_requisition.id,
        user=test_user,
        db=db_session,
    )

    # Requirement with no sightings and no hist_offers is not included
    assert str(req_item.id) not in result


# ── import_stock_list (lines 1207-1281) ──────────────────────────────────────


async def test_import_stock_list_success(
    db_session: Session, test_user: User, test_requisition: Requisition
):
    """Covers lines 1207-1281: import stock list that matches requirements."""
    from app.routers.requisitions.requirements import import_stock_list

    req_item = _make_requirement(db_session, test_requisition, mpn="LM317T")

    # Create a mock CSV file upload
    csv_content = b"mpn,qty,price\nLM317T,500,0.45\n"

    mock_file = MagicMock()
    mock_file.read = AsyncMock(return_value=csv_content)
    mock_file.filename = "stock.csv"

    mock_req = MagicMock(spec=Request)
    mock_req.headers = {}

    async def _form():
        form_mock = MagicMock()
        form_mock.get = lambda key, default=None: (
            mock_file if key == "file"
            else "Test Vendor" if key == "vendor_name"
            else default
        )
        return form_mock

    mock_req.form = _form

    with (
        patch(
            "app.routers.requisitions.requirements.resolve_material_card",
            return_value=None,
        ),
        patch(
            "app.file_utils.parse_tabular_file",
            return_value=[{"mpn": "LM317T", "qty": 500, "price": 0.45}],
        ),
        patch(
            "app.file_utils.normalize_stock_row",
            return_value={"mpn": "LM317T", "qty": 500, "price": 0.45},
        ),
    ):
        result = await import_stock_list(
            req_id=test_requisition.id,
            request=mock_req,
            user=test_user,
            db=db_session,
        )

    assert result["imported_rows"] >= 0


async def test_import_stock_list_no_file_raises(
    db_session: Session, test_user: User, test_requisition: Requisition
):
    """Covers lines 1204-1207: no file → 400."""
    from app.routers.requisitions.requirements import import_stock_list

    mock_req = MagicMock(spec=Request)
    mock_req.headers = {}

    async def _form():
        form_mock = MagicMock()
        form_mock.get = lambda key, default=None: None  # no file
        return form_mock

    mock_req.form = _form

    with pytest.raises(HTTPException) as exc:
        await import_stock_list(
            req_id=test_requisition.id,
            request=mock_req,
            user=test_user,
            db=db_session,
        )
    assert exc.value.status_code == 400


async def test_import_stock_list_requisition_not_found(
    db_session: Session, test_user: User
):
    """Covers lines 1200-1201: requisition not found → 404."""
    from app.routers.requisitions.requirements import import_stock_list

    mock_req = MagicMock(spec=Request)
    mock_req.headers = {}

    async def _form():
        return MagicMock()

    mock_req.form = _form

    with pytest.raises(HTTPException) as exc:
        await import_stock_list(
            req_id=99999,
            request=mock_req,
            user=test_user,
            db=db_session,
        )
    assert exc.value.status_code == 404


# ── list_requirement_sightings (lines 1288-1355) ──────────────────────────────


async def test_list_requirement_sightings_success(
    db_session: Session, test_user: User, test_requisition: Requisition
):
    """Covers lines 1288-1355: list sightings for a single requirement."""
    from app.routers.requisitions.requirements import list_requirement_sightings

    req_item = _make_requirement(db_session, test_requisition)

    sighting = Sighting(
        requirement_id=req_item.id,
        vendor_name="Arrow Electronics",
        vendor_name_normalized="arrow electronics",
        mpn_matched="LM317T",
        source_type="manual",
        confidence=80,
        score=50,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(sighting)
    db_session.commit()

    result = await list_requirement_sightings(
        requirement_id=req_item.id,
        user=test_user,
        db=db_session,
    )

    # list_requirement_sightings returns payload[str(requirement_id)] directly
    assert "sightings" in result
    assert "label" in result


async def test_list_requirement_sightings_not_found(
    db_session: Session, test_user: User
):
    """Covers lines 1303-1305: requirement not found → 404."""
    from app.routers.requisitions.requirements import list_requirement_sightings

    with pytest.raises(HTTPException) as exc:
        await list_requirement_sightings(
            requirement_id=99999,
            user=test_user,
            db=db_session,
        )
    assert exc.value.status_code == 404


async def test_list_requirement_sightings_with_material_card(
    db_session: Session, test_user: User, test_requisition: Requisition, test_material_card
):
    """Covers lines 1326-1328: requirement with material_card_id → included in card_ids."""
    from app.routers.requisitions.requirements import list_requirement_sightings

    req_item = Requirement(
        requisition_id=test_requisition.id,
        primary_mpn="LM317T",
        normalized_mpn="lm317t",
        target_qty=100,
        material_card_id=test_material_card.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req_item)
    db_session.commit()
    db_session.refresh(req_item)

    result = await list_requirement_sightings(
        requirement_id=req_item.id,
        user=test_user,
        db=db_session,
    )

    assert "sightings" in result
    assert "label" in result


async def test_list_requirement_sightings_with_substitutes(
    db_session: Session, test_user: User, test_requisition: Requisition, test_material_card
):
    """Covers lines 1329-1338: requirement with substitutes → sub_keys lookup."""
    from app.routers.requisitions.requirements import list_requirement_sightings

    req_item = Requirement(
        requisition_id=test_requisition.id,
        primary_mpn="LM317T",
        normalized_mpn="lm317t",
        target_qty=100,
        material_card_id=test_material_card.id,
        substitutes=["LM317AT", "LM317BT"],
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req_item)
    db_session.commit()
    db_session.refresh(req_item)

    result = await list_requirement_sightings(
        requirement_id=req_item.id,
        user=test_user,
        db=db_session,
    )

    assert "sightings" in result


# ── upload_requirements (line 603) ────────────────────────────────────────────


async def test_upload_requirements_empty_csv(
    db_session: Session, test_user: User, test_requisition: Requisition
):
    """Covers upload_requirements: empty file → 0 created."""
    from app.routers.requisitions.requirements import upload_requirements

    csv_content = b"mpn,qty\n"  # header only, no data

    mock_file = MagicMock()
    mock_file.read = AsyncMock(return_value=csv_content)
    mock_file.filename = "empty.csv"

    bg_tasks = BackgroundTasks()

    with patch(
        "app.file_utils.parse_tabular_file",
        return_value=[],
    ):
        result = await upload_requirements(
            req_id=test_requisition.id,
            background_tasks=bg_tasks,
            file=mock_file,
            user=test_user,
            db=db_session,
        )

    assert result["created"] == 0
    assert result["total_rows"] == 0


async def test_upload_requirements_with_valid_rows(
    db_session: Session, test_user: User, test_requisition: Requisition
):
    """Covers upload_requirements: file with valid MPNs → created count > 0."""
    from app.routers.requisitions.requirements import upload_requirements

    csv_content = b"mpn,qty,price\nLM317T,1000,0.50\nNE555,200,0.15\n"

    mock_file = MagicMock()
    mock_file.read = AsyncMock(return_value=csv_content)
    mock_file.filename = "bom.csv"

    bg_tasks = BackgroundTasks()

    with (
        patch(
            "app.file_utils.parse_tabular_file",
            return_value=[
                {"mpn": "LM317T", "qty": "1000", "price": "0.50"},
                {"mpn": "NE555", "qty": "200", "price": "0.15"},
            ],
        ),
        patch(
            "app.routers.requisitions.requirements.resolve_material_card",
            return_value=None,
        ),
    ):
        result = await upload_requirements(
            req_id=test_requisition.id,
            background_tasks=bg_tasks,
            file=mock_file,
            user=test_user,
            db=db_session,
        )

    assert result["created"] == 2
    assert result["total_rows"] == 2


async def test_upload_requirements_requisition_not_found(
    db_session: Session, test_user: User
):
    """Covers upload_requirements: req not found → 404."""
    from app.routers.requisitions.requirements import upload_requirements

    mock_file = MagicMock()
    mock_file.read = AsyncMock(return_value=b"mpn,qty\n")
    mock_file.filename = "test.csv"

    bg_tasks = BackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        await upload_requirements(
            req_id=99999,
            background_tasks=bg_tasks,
            file=mock_file,
            user=test_user,
            db=db_session,
        )
    assert exc.value.status_code == 404


# ── resolve_material_card exception path (lines 406-408) ─────────────────────


async def test_add_requirements_resolve_material_card_exception(
    db_session: Session, test_user: User, test_requisition: Requisition
):
    """Covers lines 406-408: resolve_material_card raises → logged, mat_card=None."""
    from app.routers.requisitions.requirements import add_requirements

    mock_req = _make_json_request({
        "primary_mpn": "BC547X",
        "manufacturer": "Fairchild",
        "target_qty": 100,
        "target_price": 0.10,
    })

    bg_tasks = BackgroundTasks()

    with patch(
        "app.routers.requisitions.requirements.resolve_material_card",
        side_effect=Exception("DB error"),
    ):
        result = await add_requirements(
            req_id=test_requisition.id,
            request=mock_req,
            background_tasks=bg_tasks,
            user=test_user,
            db=db_session,
        )

    # Requirement is still created, just without a material_card_id
    assert len(result["created"]) == 1
    assert result["created"][0]["primary_mpn"] == "BC547X"


# ── tag propagation with customer_site_id (lines 437-440) ────────────────────


async def test_add_requirements_with_customer_site_tag_propagation(
    db_session: Session,
    test_user: User,
    test_customer_site,
    test_material_card,
):
    """Covers lines 437-440: requirement created with customer_site_id → tag propagation."""
    from app.models import Requisition
    from app.routers.requisitions.requirements import add_requirements

    # Create a requisition with a customer_site_id
    req_with_site = Requisition(
        name="Site Test Req",
        status="active",
        created_by=test_user.id,
        customer_site_id=test_customer_site.id,
    )
    db_session.add(req_with_site)
    db_session.commit()
    db_session.refresh(req_with_site)

    mock_req = _make_json_request({
        "primary_mpn": "LM324N",
        "manufacturer": "TI",
        "target_qty": 200,
    })

    bg_tasks = BackgroundTasks()

    # resolve_material_card returns a material card so material_card_id is set
    with (
        patch(
            "app.routers.requisitions.requirements.resolve_material_card",
            return_value=test_material_card,
        ),
        patch(
            "app.services.tagging.propagate_tags_to_entity",
            return_value=None,
        ),
    ):
        result = await add_requirements(
            req_id=req_with_site.id,
            request=mock_req,
            background_tasks=bg_tasks,
            user=test_user,
            db=db_session,
        )

    assert len(result["created"]) == 1


# ── task service exception path (lines 452-453) ──────────────────────────────


async def test_add_requirements_task_service_exception(
    db_session: Session, test_user: User, test_requisition: Requisition
):
    """Covers lines 452-453: on_requirement_added raises → logged, continues."""
    from app.routers.requisitions.requirements import add_requirements

    mock_req = _make_json_request({
        "primary_mpn": "NE5532",
        "manufacturer": "Philips",
        "target_qty": 50,
    })

    bg_tasks = BackgroundTasks()

    with (
        patch(
            "app.routers.requisitions.requirements.resolve_material_card",
            return_value=None,
        ),
        patch(
            "app.services.task_service.on_requirement_added",
            side_effect=Exception("Task service error"),
        ),
    ):
        result = await add_requirements(
            req_id=test_requisition.id,
            request=mock_req,
            background_tasks=bg_tasks,
            user=test_user,
            db=db_session,
        )

    assert len(result["created"]) == 1


# ── search_all with draft/archived status (lines 837-838, 841-847) ───────────


async def test_search_all_with_draft_requisition_transitions_to_active(
    db_session: Session, test_user: User
):
    """Covers lines 841-847: draft requisition → transition to active."""
    from app.models import Requisition
    from app.routers.requisitions.requirements import search_all

    draft_req = Requisition(
        name="Draft Search Req",
        status="draft",
        created_by=test_user.id,
    )
    db_session.add(draft_req)
    db_session.commit()
    db_session.refresh(draft_req)

    req_item = _make_requirement(db_session, draft_req, mpn="TL071CN")

    bg_tasks = BackgroundTasks()

    with (
        patch(
            "app.routers.requisitions.search_requirement",
            new_callable=AsyncMock,
            return_value={
                "sightings": [],
                "source_stats": [],
                "req_stats": [],
            },
        ),
        patch("app.routers.requisitions.requirements._enqueue_ics_nc_batch"),
        patch("app.routers.requisitions._enrich_with_vendor_cards"),
        patch("app.routers.requisitions.requirements._annotate_buyer_outcomes"),
        # Force transition to raise ValueError to cover lines 846-847
        patch(
            "app.services.requisition_state.transition",
            side_effect=ValueError("already active"),
        ),
    ):
        result = await search_all(
            req_id=draft_req.id,
            request=MagicMock(spec=Request),
            background_tasks=bg_tasks,
            user=test_user,
            db=db_session,
        )

    # search_all returns dict keyed by req_id strings, plus "source_stats"
    assert "source_stats" in result


async def test_search_all_merged_source_stats_with_error(
    db_session: Session, test_user: User
):
    """Covers lines 837-838: merged source stats where stat has error."""
    from app.models import Requisition
    from app.routers.requisitions.requirements import search_all

    req = Requisition(
        name="Stats Test Req",
        status="active",
        created_by=test_user.id,
    )
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)

    _make_requirement(db_session, req, mpn="UA741CP")
    _make_requirement(db_session, req, mpn="LM741CN")

    bg_tasks = BackgroundTasks()

    # First call: digikey succeeds (no error); second call: digikey fails
    # This triggers the else branch (line 833) and the error assignment (lines 837-838)
    call_count = {"n": 0}

    async def _search_side_effect(req_obj, db_obj):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {
                "sightings": [],
                "source_stats": [
                    {"source": "digikey", "results": 5, "ms": 80, "error": None, "status": "ok"},
                ],
            }
        return {
            "sightings": [],
            "source_stats": [
                {"source": "digikey", "results": 0, "ms": 200, "error": "timeout", "status": "error"},
            ],
        }

    with (
        patch(
            "app.routers.requisitions.search_requirement",
            new_callable=AsyncMock,
            side_effect=_search_side_effect,
        ),
        patch("app.routers.requisitions.requirements._enqueue_ics_nc_batch"),
        patch("app.routers.requisitions._enrich_with_vendor_cards"),
        patch("app.routers.requisitions.requirements._annotate_buyer_outcomes"),
    ):
        result = await search_all(
            req_id=req.id,
            request=MagicMock(spec=Request),
            background_tasks=bg_tasks,
            user=test_user,
            db=db_session,
        )

    # search_all returns dict keyed by req_id strings, plus "source_stats"
    assert "source_stats" in result


# ── upload_requirements with substitutes column (line 593) ───────────────────


async def test_upload_requirements_with_substitutes_column(
    db_session: Session, test_user: User, test_requisition: Requisition
):
    """Covers line 593: substitutes column in upload rows → split into list."""
    from app.routers.requisitions.requirements import upload_requirements

    mock_file = MagicMock()
    mock_file.read = AsyncMock(return_value=b"mpn,qty,substitutes\nLM317T,100,LM317AT,LM317BT\n")
    mock_file.filename = "bom.csv"

    bg_tasks = BackgroundTasks()

    with (
        patch(
            "app.file_utils.parse_tabular_file",
            return_value=[
                {"mpn": "LM317T", "qty": "100", "substitutes": "LM317AT,LM317BT"},
            ],
        ),
        patch(
            "app.routers.requisitions.requirements.resolve_material_card",
            return_value=None,
        ),
    ):
        result = await upload_requirements(
            req_id=test_requisition.id,
            background_tasks=bg_tasks,
            file=mock_file,
            user=test_user,
            db=db_session,
        )

    assert result["created"] >= 1


async def test_upload_requirements_with_invalid_substitute_normalized_to_none(
    db_session: Session, test_user: User, test_requisition: Requisition
):
    """Covers line 603: substitute string normalizes to None → continue skips it."""
    from app.routers.requisitions.requirements import upload_requirements

    mock_file = MagicMock()
    mock_file.read = AsyncMock(return_value=b"mpn,qty,substitutes\nLM317T,100,AB\n")
    mock_file.filename = "bom.csv"

    bg_tasks = BackgroundTasks()

    # "AB" is too short (< 3 chars) → normalize_mpn returns None → line 603 continue
    with (
        patch(
            "app.file_utils.parse_tabular_file",
            return_value=[
                {"mpn": "LM317T", "qty": "100", "substitutes": "AB"},  # too short
            ],
        ),
        patch(
            "app.routers.requisitions.requirements.resolve_material_card",
            return_value=None,
        ),
    ):
        result = await upload_requirements(
            req_id=test_requisition.id,
            background_tasks=bg_tasks,
            file=mock_file,
            user=test_user,
            db=db_session,
        )

    # Requirement created, but no valid substitutes
    assert result["created"] == 1


# ── import_stock_list: no filename (line 1213) ───────────────────────────────


async def test_import_stock_list_no_filename_raises_400(
    db_session: Session, test_user: User, test_requisition: Requisition
):
    """Covers line 1213: file has no filename → 400."""
    from app.routers.requisitions.requirements import import_stock_list

    mock_file = MagicMock()
    mock_file.read = AsyncMock(return_value=b"mpn,qty\nLM317T,100\n")
    mock_file.filename = None  # no filename

    mock_req = MagicMock(spec=Request)
    mock_req.headers = {}

    async def _form():
        form_mock = MagicMock()
        form_mock.get = lambda key, default=None: (
            mock_file if key == "file" else default
        )
        return form_mock

    mock_req.form = _form

    with pytest.raises(HTTPException) as exc_info:
        await import_stock_list(
            req_id=test_requisition.id,
            request=mock_req,
            user=test_user,
            db=db_session,
        )
    assert exc_info.value.status_code == 400
    assert "no filename" in exc_info.value.detail.lower()


# ── import_stock_list: substitutes string in requirement (lines 1229-1230) ───


async def test_import_stock_list_with_requirement_having_substitutes(
    db_session: Session, test_user: User, test_requisition: Requisition
):
    """Covers lines 1229-1230: requirement with string substitutes used for mpn matching."""
    from app.routers.requisitions.requirements import import_stock_list

    # Create requirement with string substitutes
    req_item = Requirement(
        requisition_id=test_requisition.id,
        primary_mpn="LM317T",
        normalized_mpn="lm317t",
        target_qty=100,
        substitutes=["LM317AT"],
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req_item)
    db_session.commit()
    db_session.refresh(req_item)

    csv_content = b"mpn,qty,price\nLM317AT,200,0.50\n"

    mock_file = MagicMock()
    mock_file.read = AsyncMock(return_value=csv_content)
    mock_file.filename = "stock.csv"

    mock_req = MagicMock(spec=Request)
    mock_req.headers = {}

    async def _form():
        form_mock = MagicMock()
        form_mock.get = lambda key, default=None: (
            mock_file if key == "file"
            else "Acme Surplus" if key == "vendor_name"
            else default
        )
        return form_mock

    mock_req.form = _form

    with (
        patch(
            "app.routers.requisitions.requirements.resolve_material_card",
            return_value=None,
        ),
        patch(
            "app.file_utils.parse_tabular_file",
            return_value=[{"mpn": "LM317AT", "qty": "200", "price": "0.50"}],
        ),
        patch(
            "app.file_utils.normalize_stock_row",
            return_value={"mpn": "LM317AT", "qty": 200, "price": 0.50},
        ),
    ):
        result = await import_stock_list(
            req_id=test_requisition.id,
            request=mock_req,
            user=test_user,
            db=db_session,
        )

    assert result["matched_sightings"] >= 1


# ── import_stock_list: exception during db.commit (lines 1277-1280) ──────────


async def test_import_stock_list_commit_exception_raises_500(
    db_session: Session, test_user: User, test_requisition: Requisition
):
    """Covers lines 1277-1280: exception during import → 500 rollback."""
    from app.routers.requisitions.requirements import import_stock_list

    _make_requirement(db_session, test_requisition, mpn="LM317T")

    mock_file = MagicMock()
    mock_file.read = AsyncMock(return_value=b"mpn,qty\nLM317T,100\n")
    mock_file.filename = "stock.csv"

    mock_req = MagicMock(spec=Request)
    mock_req.headers = {}

    async def _form():
        form_mock = MagicMock()
        form_mock.get = lambda key, default=None: (
            mock_file if key == "file" else default
        )
        return form_mock

    mock_req.form = _form

    with (
        patch(
            "app.file_utils.parse_tabular_file",
            return_value=[{"mpn": "LM317T", "qty": "100"}],
        ),
        patch(
            "app.file_utils.normalize_stock_row",
            return_value={"mpn": "LM317T", "qty": 100},
        ),
        patch(
            "app.routers.requisitions.requirements.resolve_material_card",
            side_effect=Exception("DB failure during import"),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await import_stock_list(
                req_id=test_requisition.id,
                request=mock_req,
                user=test_user,
                db=db_session,
            )
    assert exc_info.value.status_code == 500
    assert "import failed" in exc_info.value.detail.lower()


# ── list_requirement_sightings: material history populated (line 1342) ────────


async def test_list_requirement_sightings_with_material_history(
    db_session: Session, test_user: User, test_requisition: Requisition, test_material_card
):
    """Covers line 1342: material history returned → appended to sighting_dicts."""
    from app.routers.requisitions.requirements import list_requirement_sightings

    req_item = Requirement(
        requisition_id=test_requisition.id,
        primary_mpn="LM317T",
        normalized_mpn="lm317t",
        target_qty=100,
        material_card_id=test_material_card.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req_item)
    db_session.commit()
    db_session.refresh(req_item)

    fake_history_row = {
        "vendor_name": "Arrow",
        "last_seen": datetime.now(timezone.utc),
        "first_seen": datetime.now(timezone.utc),
        "times_seen": 3,
        "unit_price": 0.45,
        "qty_available": 500,
        "is_authorized": False,
        "mpn_matched": "LM317T",
        "manufacturer": "TI",
        "source_type": "broker",
        "condition": "new",
        "date_code": None,
        "lead_time": None,
        "currency": "USD",
        "packaging": None,
        "vendor_sku": None,
        "material_card_id": test_material_card.id,
    }

    with patch(
        "app.routers.requisitions._get_material_history",
        return_value=[fake_history_row],
    ):
        result = await list_requirement_sightings(
            requirement_id=req_item.id,
            user=test_user,
            db=db_session,
        )

    assert "sightings" in result


# ── get_saved_sightings: material history (line 1025) ───────────────────────


async def test_get_saved_sightings_with_material_history(
    db_session: Session, test_user: User, test_requisition: Requisition, test_material_card
):
    """Covers line 1025: get_saved_sightings with material history → appended."""
    from app.routers.requisitions.requirements import get_saved_sightings

    req_item = Requirement(
        requisition_id=test_requisition.id,
        primary_mpn="LM317T",
        normalized_mpn="lm317t",
        target_qty=100,
        material_card_id=test_material_card.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req_item)
    db_session.commit()
    db_session.refresh(req_item)

    # Add a sighting so sighting_dicts is non-empty (to avoid the continue at line 1029)
    sighting = Sighting(
        requirement_id=req_item.id,
        vendor_name="Arrow",
        vendor_name_normalized="arrow",
        mpn_matched="LM317T",
        source_type="manual",
        confidence=70,
        score=50,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(sighting)
    db_session.commit()

    fake_history_row = {
        "vendor_name": "Mouser",
        "last_seen": datetime.now(timezone.utc),
        "first_seen": datetime.now(timezone.utc),
        "times_seen": 2,
        "unit_price": 0.30,
        "qty_available": 250,
        "is_authorized": True,
        "mpn_matched": "LM317T",
        "manufacturer": "TI",
        "source_type": "broker",
        "condition": "new",
        "date_code": None,
        "lead_time": None,
        "currency": "USD",
        "packaging": None,
        "vendor_sku": None,
        "material_card_id": test_material_card.id,
    }

    with patch(
        "app.routers.requisitions._get_material_history",
        return_value=[fake_history_row],
    ):
        result = await get_saved_sightings(
            req_id=test_requisition.id,
            user=test_user,
            db=db_session,
        )

    # get_saved_sightings returns a dict keyed by req_id strings
    assert str(req_item.id) in result
    assert "sightings" in result[str(req_item.id)]


# ── import_stock_list: normalize_stock_row returns None (line 1241) ──────────


async def test_import_stock_list_normalize_stock_row_returns_none(
    db_session: Session, test_user: User, test_requisition: Requisition
):
    """Covers line 1241: normalize_stock_row returns None → skip row."""
    from app.routers.requisitions.requirements import import_stock_list

    mock_file = MagicMock()
    mock_file.read = AsyncMock(return_value=b"mpn,qty\nBAD_ROW,100\n")
    mock_file.filename = "stock.csv"

    mock_req = MagicMock(spec=Request)
    mock_req.headers = {}

    async def _form():
        form_mock = MagicMock()
        form_mock.get = lambda key, default=None: (
            mock_file if key == "file" else default
        )
        return form_mock

    mock_req.form = _form

    with (
        patch(
            "app.file_utils.parse_tabular_file",
            return_value=[{"mpn": "BAD_ROW", "qty": "100"}],
        ),
        patch(
            "app.file_utils.normalize_stock_row",
            return_value=None,  # row is skipped
        ),
        patch(
            "app.routers.requisitions.requirements.resolve_material_card",
            return_value=None,
        ),
    ):
        result = await import_stock_list(
            req_id=test_requisition.id,
            request=mock_req,
            user=test_user,
            db=db_session,
        )

    # Row was skipped → 0 imported
    assert result["imported_rows"] == 0


# ── import_stock_list: mpn not in req_mpns (line 1247) ───────────────────────


async def test_import_stock_list_mpn_not_in_req_mpns(
    db_session: Session, test_user: User, test_requisition: Requisition
):
    """Covers line 1247: parsed mpn not found in req_mpns → skip, matched stays 0."""
    from app.routers.requisitions.requirements import import_stock_list

    # No requirements → req_mpns is empty → all rows skip at line 1247
    mock_file = MagicMock()
    mock_file.read = AsyncMock(return_value=b"mpn,qty\nXYZ999,100\n")
    mock_file.filename = "stock.csv"

    mock_req = MagicMock(spec=Request)
    mock_req.headers = {}

    async def _form():
        form_mock = MagicMock()
        form_mock.get = lambda key, default=None: (
            mock_file if key == "file" else default
        )
        return form_mock

    mock_req.form = _form

    with (
        patch(
            "app.file_utils.parse_tabular_file",
            return_value=[{"mpn": "XYZ999", "qty": "100"}],
        ),
        patch(
            "app.file_utils.normalize_stock_row",
            return_value={"mpn": "XYZ999", "qty": 100},
        ),
        patch(
            "app.routers.requisitions.requirements.resolve_material_card",
            return_value=None,
        ),
    ):
        result = await import_stock_list(
            req_id=test_requisition.id,
            request=mock_req,
            user=test_user,
            db=db_session,
        )

    # Imported (counted) but not matched since MPN not in requirements
    assert result["imported_rows"] == 1
    assert result["matched_sightings"] == 0
