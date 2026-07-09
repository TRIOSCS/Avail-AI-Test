"""Regression tests for the search-sourcing P2 workflow-break cluster (2026-07-02
production-polish audit).

Covers:
  - MAT-TABS-CLICK-ONCE     — material detail tabs must re-fetch on every click
                              (no `hx-trigger="click once"`), so re-visiting a tab
                              reloads its content instead of showing the last tab.
  - SOURCING-WS-ALPINE-INIT — the `sourcingWorkspace` Alpine component must be
                              registered statically in htmx_app.js (an in-partial
                              `alpine:init` listener never re-fires after Alpine.start,
                              so x-data would throw and keyboard nav would be dead).
  - SOURCING-LEGACY-HREF-404 — grid/workspace/lead toggle links must push the
                              registered `/v2/sourcing/...` full-page routes, not the
                              never-registered legacy `/sourcing/...` paths (F5 / new-tab
                              404).
  - DOSSIER-ADDREQ-EMPTY-400 — "Add to Requisition" with nothing shortlisted must add
                              the PART (create the Requirement) rather than dead-ending
                              on a 400.

What calls it: pytest.
Depends on: app.routers.htmx.sourcing (results/workspace partials),
            app.routers.htmx.materials (detail partial),
            app.routers.htmx_views.add_to_requisition, conftest fixtures.
"""

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import RequisitionStatus, SourcingStatus
from app.models import MaterialCard, Requirement, Requisition, User


# ── Helpers ────────────────────────────────────────────────────────────────
def _requisition(db: Session, user: User) -> Requisition:
    req = Requisition(
        name="SS-P2-REQ",
        customer_name="Acme",
        status=RequisitionStatus.OPEN,
        created_by=user.id,
        claimed_by_id=user.id,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()
    return req


def _requirement(db: Session, req: Requisition, mpn: str = "LM317T") -> Requirement:
    r = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        normalized_mpn=mpn.lower(),
        target_qty=100,
        sourcing_status=SourcingStatus.OPEN,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _card(db: Session, mpn: str = "LM317T") -> MaterialCard:
    card = MaterialCard(normalized_mpn=mpn.lower(), display_mpn=mpn, manufacturer="TI", search_count=0)
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


# ── MAT-TABS-CLICK-ONCE ─────────────────────────────────────────────────────
def test_material_tabs_refetch_on_every_click(client: TestClient, db_session: Session):
    """Detail tabs must use plain `click` (re-fetch each click), never `click once`
    (which strands the shared container on the last-loaded tab)."""
    card = _card(db_session)
    resp = client.get(f"/v2/partials/materials/{card.id}")
    assert resp.status_code == 200
    html = resp.text
    assert "click once" not in html, "material detail tabs still use hx-trigger='click once'"
    # Vendors tab auto-loads then re-fetches on click; others fetch on click.
    assert 'hx-trigger="load, click"' in html
    assert 'hx-trigger="click"' in html


# ── SOURCING-WS-ALPINE-INIT ─────────────────────────────────────────────────
def test_sourcing_workspace_alpine_registered_statically(client: TestClient, db_session: Session, test_user: User):
    """The workspace partial must invoke sourcingWorkspace(...) with args and carry no
    in-partial `alpine:init` registration; the component lives in htmx_app.js."""
    req = _requisition(db_session, test_user)
    item = _requirement(db_session, req)
    resp = client.get(f"/v2/partials/sourcing/{item.id}/workspace")
    assert resp.status_code == 200
    html = resp.text
    # x-data now passes the initial selection + lead ids as args (static factory pattern).
    assert "sourcingWorkspace(" in html
    # The stranded pattern is gone: no partial-scoped alpine:init registration.
    assert "alpine:init" not in html

    # The component is registered statically (like splitPanel) so it exists before
    # Alpine processes the HTMX-swapped x-data.
    js = Path("app/static/htmx_app.js").read_text()
    assert "Alpine.data('sourcingWorkspace'" in js


# ── SOURCING-LEGACY-HREF-404 ────────────────────────────────────────────────
def test_sourcing_workspace_grid_link_uses_v2(client: TestClient, db_session: Session, test_user: User):
    """The workspace's Grid-view toggle must push the registered /v2/sourcing/{id}."""
    req = _requisition(db_session, test_user)
    item = _requirement(db_session, req)
    html = client.get(f"/v2/partials/sourcing/{item.id}/workspace").text
    assert f'hx-push-url="/v2/sourcing/{item.id}"' in html
    assert f'href="/v2/sourcing/{item.id}"' in html
    # No legacy, unregistered /sourcing/{id} (404 on reload/new-tab).
    assert 'hx-push-url="/sourcing/' not in html
    assert 'href="/sourcing/' not in html


def test_sourcing_results_workspace_link_uses_v2(client: TestClient, db_session: Session, test_user: User):
    """The results view's Workspace-view toggle must push
    /v2/sourcing/{id}/workspace."""
    req = _requisition(db_session, test_user)
    item = _requirement(db_session, req)
    html = client.get(f"/v2/partials/sourcing/{item.id}").text
    assert f'hx-push-url="/v2/sourcing/{item.id}/workspace"' in html
    assert 'hx-push-url="/sourcing/' not in html
    assert 'href="/sourcing/' not in html


# ── DOSSIER-ADDREQ-EMPTY-400 ────────────────────────────────────────────────
def test_add_to_requisition_empty_items_adds_part(client: TestClient, db_session: Session, test_user: User):
    """From the dossier with nothing shortlisted, Add-to-Requisition must add the PART
    (create the Requirement) and return 200 — not dead-end on 'Missing required
    fields'."""
    req = _requisition(db_session, test_user)
    db_session.commit()

    resp = client.post(
        "/v2/partials/search/add-to-requisition",
        json={"requisition_id": req.id, "mpn": "NE555P", "items": []},
    )
    assert resp.status_code == 200
    assert "NE555P" in resp.text
    requirement = db_session.query(Requirement).filter_by(requisition_id=req.id, primary_mpn="NE555P").one()
    assert requirement.sourcing_status == SourcingStatus.OPEN


def test_add_to_requisition_still_requires_mpn(client: TestClient, db_session: Session, test_user: User):
    """The MPN remains required — a blank MPN is still a 400 (the guard only dropped the
    non-empty-items condition, not the MPN key)."""
    req = _requisition(db_session, test_user)
    db_session.commit()
    resp = client.post(
        "/v2/partials/search/add-to-requisition",
        json={"requisition_id": req.id, "mpn": "", "items": []},
    )
    assert resp.status_code == 400
