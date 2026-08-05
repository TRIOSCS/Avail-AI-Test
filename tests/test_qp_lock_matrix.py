"""test_qp_lock_matrix.py — W3.7: the ONE QP section lock matrix + dropped ceremony.

Covers:
  - can_edit_qp_section (services/qp_workspace): table-driven stage × section × actor →
    editable?, for every BuyPlanStatus, both sections, and the owner / other /
    manager / admin actors — plus the no-linked-plan and unknown-section locks.
  - can_edit_qp_sales / can_edit_qp_purchasing delegate to the same matrix.
  - The two self-stamped Mark-Reviewed routes are GONE: absent from the route registry
    and a live POST 404s (test_inventory_cleanup pattern).

Called by: pytest
Depends on: app.services.qp_workspace, app.main (route registry),
            tests._route_helpers (iter_routes), conftest (client).
"""

from types import SimpleNamespace

import pytest

from app.main import app
from app.services.qp_workspace import (
    can_edit_qp_purchasing,
    can_edit_qp_sales,
    can_edit_qp_section,
)
from tests._route_helpers import iter_routes

# ── Actors / plans (lightweight fakes — the matrix reads only role/id/status/req) ──

OWNER_ID = 7

ACTORS = {
    "owner": SimpleNamespace(id=OWNER_ID, role="sales"),  # the requisition creator
    "other": SimpleNamespace(id=99, role="buyer"),  # passed ownership scope, no role
    "manager": SimpleNamespace(id=2, role="manager"),
    "admin": SimpleNamespace(id=3, role="admin"),
}


def _plan(status: str) -> SimpleNamespace:
    return SimpleNamespace(status=status, requisition=SimpleNamespace(created_by=OWNER_ID))


# ── The matrix, pinned case by case (stage × section × actor → editable?) ──────────

MATRIX_CASES = [
    # draft: sales → owner or manager/admin; purchasing → locked
    ("draft", "sales", "owner", True),
    ("draft", "sales", "other", False),
    ("draft", "sales", "manager", True),
    ("draft", "sales", "admin", True),
    ("draft", "purchasing", "owner", False),
    ("draft", "purchasing", "manager", False),
    ("draft", "purchasing", "admin", False),
    # pending: sales → manager/admin only; purchasing → locked
    ("pending", "sales", "owner", False),
    ("pending", "sales", "other", False),
    ("pending", "sales", "manager", True),
    ("pending", "sales", "admin", True),
    ("pending", "purchasing", "manager", False),
    ("pending", "purchasing", "admin", False),
    # active: sales → locked (even for admins); purchasing → open (confirm-PO window)
    ("active", "sales", "owner", False),
    ("active", "sales", "manager", False),
    ("active", "sales", "admin", False),
    ("active", "purchasing", "owner", True),
    ("active", "purchasing", "other", True),
    ("active", "purchasing", "manager", True),
    ("active", "purchasing", "admin", True),
    # terminal / parked stages: everything locked
    ("halted", "sales", "admin", False),
    ("halted", "purchasing", "admin", False),
    ("completed", "sales", "manager", False),
    ("completed", "purchasing", "manager", False),
    ("cancelled", "sales", "admin", False),
    ("cancelled", "purchasing", "admin", False),
]


@pytest.mark.parametrize(("status", "section", "actor", "expected"), MATRIX_CASES)
def test_lock_matrix(status: str, section: str, actor: str, expected: bool) -> None:
    """Stage × section × actor resolves exactly per the ONE lock matrix."""
    assert can_edit_qp_section(ACTORS[actor], _plan(status), section) is expected


def test_no_linked_plan_locks_both_sections() -> None:
    """A QP with no linked buy plan is locked for everyone, both sections."""
    admin = ACTORS["admin"]
    assert can_edit_qp_section(admin, None, "sales") is False
    assert can_edit_qp_section(admin, None, "purchasing") is False


def test_unknown_section_is_locked() -> None:
    """A section name outside sales/purchasing never unlocks."""
    assert can_edit_qp_section(ACTORS["admin"], _plan("draft"), "serial") is False


def test_named_shorthands_delegate_to_matrix() -> None:
    """can_edit_qp_sales / can_edit_qp_purchasing are the same matrix, not a fork."""
    manager = ACTORS["manager"]
    assert can_edit_qp_sales(manager, _plan("draft")) is True
    assert can_edit_qp_purchasing(manager, _plan("draft")) is False
    assert can_edit_qp_sales(manager, _plan("active")) is False
    assert can_edit_qp_purchasing(manager, _plan("active")) is True


# ── The Mark-Reviewed ceremony routes are gone (W3.7) ─────────────────────────────


def _paths() -> set[str]:
    """Set of registered route paths (flattened through included-router wrappers)."""
    return {getattr(route, "path", None) for route in iter_routes(app.routes)}


def test_qp_review_routes_removed_from_registry() -> None:
    """The two self-stamped review toggles are no longer registered routes."""
    paths = _paths()
    assert "/v2/qp/{qp_id}/sales/review" not in paths
    assert "/v2/qp/{qp_id}/purchasing/review" not in paths


def test_qp_review_post_404s(client) -> None:
    """A live POST to either former review toggle 404s (route gone, not 405/500)."""
    for section in ("sales", "purchasing"):
        resp = client.post(f"/v2/qp/1/{section}/review", data={"action": "mark"})
        assert resp.status_code == 404
