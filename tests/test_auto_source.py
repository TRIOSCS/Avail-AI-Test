"""
tests/test_auto_source.py — Tests for auto-sourcing on requirement creation

Covers: background task enqueue for search_requirement when requirements are
added via POST (single, batch) and file upload.

Called by: pytest
Depends on: routers/requisitions/requirements.py, conftest fixtures
"""

from unittest.mock import AsyncMock, patch

# ── Auto-source triggers on requirement creation ─────────────────────


def test_add_requirement_triggers_auto_source(client, test_requisition):
    """POST /api/requisitions/{id}/requirements enqueues auto-source background task."""
    with patch(
        "app.routers.requisitions.requirements.BackgroundTasks.add_task"
    ) as mock_add:
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/requirements",
            json={"primary_mpn": "STM32F407VG", "target_qty": 100},
        )
    assert resp.status_code == 200
    assert len(resp.json()["created"]) == 1
    # Verify auto-source task was enqueued alongside NC/ICS
    task_names = [call.args[0].__name__ for call in mock_add.call_args_list]
    assert "_auto_source_batch" in task_names


def test_add_requirement_batch_triggers_auto_source(client, test_requisition):
    """Batch POST enqueues one auto-source call covering all created requirements."""
    with patch(
        "app.routers.requisitions.requirements.BackgroundTasks.add_task"
    ) as mock_add:
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/requirements",
            json=[
                {"primary_mpn": "LM7805", "target_qty": 50},
                {"primary_mpn": "LM7812", "target_qty": 200},
            ],
        )
    assert resp.status_code == 200
    assert len(resp.json()["created"]) == 2
    # Find the auto-source call
    auto_calls = [
        call for call in mock_add.call_args_list
        if call.args[0].__name__ == "_auto_source_batch"
    ]
    assert len(auto_calls) == 1
    # The requirement IDs list should have 2 entries
    req_ids = auto_calls[0].args[1]
    assert len(req_ids) == 2


def test_add_requirement_no_auto_source_when_all_skipped(client, test_requisition):
    """No auto-source task enqueued when all items fail validation."""
    with patch(
        "app.routers.requisitions.requirements.BackgroundTasks.add_task"
    ) as mock_add:
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/requirements",
            json=[{"primary_mpn": "", "target_qty": 1}],
        )
    assert resp.status_code == 200
    assert len(resp.json()["created"]) == 0
    # No background tasks should be enqueued
    task_names = [call.args[0].__name__ for call in mock_add.call_args_list]
    assert "_auto_source_batch" not in task_names


# ── Frontend: post-creation tab is workspace ─────────────────────────


def test_post_creation_tab_is_workspace():
    """After creating a requisition, the UI should expand to 'workspace' tab."""
    with open("app/static/app.js") as f:
        js = f.read()
    fn_start = js.find("async function createRequisition()")
    assert fn_start > 0
    fn_end = js.find("\nfunction ", fn_start + 1)
    fn_body = js[fn_start:fn_end]
    assert "expandToSubTab(data.id, 'workspace')" in fn_body
    assert "expandToSubTab(data.id, 'sightings')" not in fn_body


# ── Workspace empty state has add-part link ──────────────────────────


def test_workspace_empty_state_has_add_part():
    """Workspace shows clickable 'Add Part' link when no parts exist."""
    with open("app/static/app.js") as f:
        js = f.read()
    fn_start = js.find("function _rfqRenderPartList(")
    assert fn_start > 0
    fn_end = js.find("\nfunction ", fn_start + 1)
    fn_body = js[fn_start:fn_end]
    assert "addDrillRow" in fn_body
    assert "+ Add Part" in fn_body
