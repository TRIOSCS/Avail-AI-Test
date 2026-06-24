"""Tests for archive-DNC feature (migration 148).

Covers:
- Archive account: unassigns owner, hides from active list, searchable
- Reactivate: manager/admin only (stricter gate than current can_manage_account_team)
- Archived browse view (/v2/partials/customers/archived)
- Site DNC toggle (POST mark-dnc)
- Migration 148 single head
- site_card.html template: Delete Site removed

Called by: pytest
Depends on: conftest.py fixtures, app.models.crm (Company, CustomerSite), app.routers.htmx_views
"""

import pathlib

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, User

# ── Helper ─────────────────────────────────────────────────────────────


def _make_client(db_session: Session, user: User) -> TestClient:
    """Build a TestClient that authenticates as `user`, wired to `db_session`."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    async def _fresh():
        return "mock-token"

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user
    app.dependency_overrides[require_buyer] = lambda: user
    app.dependency_overrides[require_fresh_token] = _fresh
    tc = TestClient(app, raise_server_exceptions=False)
    return tc


def _cleanup_overrides():
    """Remove all dependency_overrides to keep tests isolated."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    for dep in (get_db, require_user, require_admin, require_buyer, require_fresh_token):
        app.dependency_overrides.pop(dep, None)


# ── 1. Archive sets is_active=False, clears owner, stores reason ───────


def test_archive_clears_owner_and_sets_inactive(
    db_session: Session,
    test_company: Company,
    test_user: User,
):
    """POST /deactivate → is_active=False, account_owner_id=None, ownership_cleared_at
    set, disposition_reason stored from form."""
    test_company.account_owner_id = test_user.id
    db_session.commit()

    c = _make_client(db_session, test_user)
    try:
        resp = c.post(
            f"/v2/partials/customers/{test_company.id}/deactivate",
            data={"disposition_reason": "test reason"},
        )
    finally:
        _cleanup_overrides()

    assert resp.status_code == 200

    db_session.expire(test_company)
    assert test_company.is_active is False
    assert test_company.account_owner_id is None
    assert test_company.ownership_cleared_at is not None
    assert test_company.disposition_reason == "test reason"


# ── 2. Archived company hidden from active list ─────────────────────────


def test_archive_unassigns_and_hides_from_active_list(
    db_session: Session,
    test_company: Company,
    test_user: User,
):
    """Archive a company, then GET account-list — company name must NOT appear."""
    test_company.account_owner_id = test_user.id
    db_session.commit()

    c = _make_client(db_session, test_user)
    try:
        c.post(
            f"/v2/partials/customers/{test_company.id}/deactivate",
            data={"disposition_reason": "spam"},
        )
        db_session.expire(test_company)

        resp = c.get("/v2/partials/customers/account-list")
    finally:
        _cleanup_overrides()

    assert resp.status_code == 200
    assert "Acme Electronics" not in resp.text


# ── 3. Sales rep cannot reactivate ─────────────────────────────────────


def test_reactivate_rep_denied(
    db_session: Session,
    test_company: Company,
    sales_user: User,
):
    """Reactivate gate is now is_manager_or_admin; sales rep gets 403."""
    test_company.is_active = False
    test_company.account_owner_id = sales_user.id
    db_session.commit()

    c = _make_client(db_session, sales_user)
    try:
        resp = c.post(f"/v2/partials/customers/{test_company.id}/reactivate")
    finally:
        _cleanup_overrides()

    assert resp.status_code == 403


# ── 4. Manager can reactivate ───────────────────────────────────────────


def test_reactivate_mgr_allowed(
    db_session: Session,
    test_company: Company,
    manager_user: User,
):
    """Manager (is_manager_or_admin) can reactivate an archived company."""
    test_company.is_active = False
    db_session.commit()

    c = _make_client(db_session, manager_user)
    try:
        resp = c.post(f"/v2/partials/customers/{test_company.id}/reactivate")
    finally:
        _cleanup_overrides()

    assert resp.status_code == 200


# ── 5. Archived browse view lists archived companies ───────────────────


def test_archived_browse_view_lists_archived(
    db_session: Session,
    test_company: Company,
    test_user: User,
):
    """GET /v2/partials/customers/archived → 200 with archived company name."""
    test_company.is_active = False
    db_session.commit()

    c = _make_client(db_session, test_user)
    try:
        resp = c.get("/v2/partials/customers/archived")
    finally:
        _cleanup_overrides()

    assert resp.status_code == 200
    assert "Acme Electronics" in resp.text


# ── 6. Name search returns archived with badge ─────────────────────────


def test_name_search_returns_archived_with_badge(
    db_session: Session,
    test_company: Company,
    test_user: User,
):
    """Search by name includes archived companies and badges them as 'Archived'."""
    test_company.is_active = False
    db_session.commit()

    c = _make_client(db_session, test_user)
    try:
        resp = c.get("/v2/partials/customers/account-list?search=Acme")
    finally:
        _cleanup_overrides()

    assert resp.status_code == 200
    assert "Archived" in resp.text


# ── 7. Default active list excludes archived ───────────────────────────


def test_name_search_active_list_excludes_archived_from_default(
    db_session: Session,
    test_company: Company,
    test_user: User,
):
    """GET account-list with no search — archived company must NOT appear."""
    test_company.is_active = False
    db_session.commit()

    c = _make_client(db_session, test_user)
    try:
        resp = c.get("/v2/partials/customers/account-list")
    finally:
        _cleanup_overrides()

    assert resp.status_code == 200
    assert "Acme Electronics" not in resp.text


# ── 8. Migration 148 is the single alembic head ────────────────────────


def test_migration_148_single_head():
    """Alembic must have exactly one head, and 148_site_dnc must stay in the chain.

    148_site_dnc may no longer BE the head once a later migration chains onto it (e.g.
    149_user_mgmt) — the invariant that matters is no multiple-head fork, plus that the
    archive-dnc revision remains reachable from the single head.
    """
    alembic_dir = pathlib.Path(__file__).resolve().parent.parent / "alembic"
    from alembic.script import ScriptDirectory

    script_dir = ScriptDirectory(str(alembic_dir))
    heads = script_dir.get_heads()
    assert len(heads) == 1, f"Expected 1 alembic head, got {heads}"
    chain = {rev.revision for rev in script_dir.walk_revisions()}
    assert "148_site_dnc" in chain, f"148_site_dnc missing from the revision chain {sorted(chain)}"


# ── 9. Site mark-dnc toggles do_not_contact ────────────────────────────


def test_site_mark_dnc_toggles(
    db_session: Session,
    test_company: Company,
    test_customer_site: CustomerSite,
    test_user: User,
):
    """POST mark-dnc → site.do_not_contact becomes True."""
    test_company.account_owner_id = test_user.id
    db_session.commit()

    c = _make_client(db_session, test_user)
    try:
        resp = c.post(f"/v2/partials/customers/{test_company.id}/sites/{test_customer_site.id}/mark-dnc")
    finally:
        _cleanup_overrides()

    assert resp.status_code == 200
    db_session.expire(test_customer_site)
    assert test_customer_site.do_not_contact is True


# ── 10. DNC site excluded from needs_call surfaces ─────────────────────


def test_site_dnc_excluded_from_call_surfaces(
    db_session: Session,
    test_company: Company,
    test_customer_site: CustomerSite,
    test_user: User,
):
    """Mark site DNC → company should not appear in staleness=needs_call list."""
    test_customer_site.do_not_contact = True
    # Company needs to be contactable-age (no recent activity → needs_call band)
    test_company.last_activity_at = None
    db_session.commit()

    c = _make_client(db_session, test_user)
    try:
        resp = c.get("/v2/partials/customers/account-list?staleness=needs_call")
    finally:
        _cleanup_overrides()

    assert resp.status_code == 200
    assert "Acme Electronics" not in resp.text


# ── 11. DNC gate: unrelated sales user denied ──────────────────────────


def test_site_dnc_gate_deny(
    db_session: Session,
    test_company: Company,
    test_customer_site: CustomerSite,
    sales_user: User,
):
    """Sales user who does not own test_company is denied mark-dnc (403)."""
    # test_company has no owner → can_manage_account returns False for sales_user
    test_company.account_owner_id = None
    db_session.commit()

    c = _make_client(db_session, sales_user)
    try:
        resp = c.post(f"/v2/partials/customers/{test_company.id}/sites/{test_customer_site.id}/mark-dnc")
    finally:
        _cleanup_overrides()

    assert resp.status_code == 403


# ── 12. site_card.html no longer has "Delete Site" ─────────────────────


def test_site_delete_action_gone():
    """Template site_card.html must NOT contain the Delete Site action."""
    tpl = pathlib.Path(__file__).resolve().parent.parent / "app/templates/htmx/partials/customers/tabs/site_card.html"
    text = tpl.read_text()
    assert "Delete Site" not in text, "Delete Site action should have been removed from site_card.html"


# ── 13. C2 (IDOR) — delete_site must deny unrelated reps ──────────────


def test_delete_site_idor_deny(
    db_session: Session,
    test_company: Company,
    test_customer_site: "CustomerSite",
    sales_user: "User",
):
    """DELETE /sites/{site_id} by an unrelated rep must return 403, not soft-delete."""
    # sales_user does not own test_company → can_manage_account returns False
    test_company.account_owner_id = None
    db_session.commit()

    c = _make_client(db_session, sales_user)
    try:
        resp = c.delete(f"/v2/partials/customers/{test_company.id}/sites/{test_customer_site.id}")
    finally:
        _cleanup_overrides()

    assert resp.status_code == 403, f"Expected 403, got {resp.status_code}"

    # Site must NOT have been deleted
    db_session.expire(test_customer_site)
    assert test_customer_site.is_active is True, "Site should not have been soft-deleted"


# ── 14. I1 — company_detail_partial passes can_reactivate context ──────


def test_detail_partial_passes_can_reactivate_to_template(
    db_session: Session,
    test_company: Company,
    manager_user: "User",
):
    """GET company_detail_partial as manager: response must include Reactivate button."""
    test_company.is_active = False
    db_session.commit()

    c = _make_client(db_session, manager_user)
    try:
        resp = c.get(f"/v2/partials/customers/{test_company.id}")
    finally:
        _cleanup_overrides()

    assert resp.status_code == 200
    assert "Reactivate" in resp.text, "Manager should see Reactivate button in archived detail"


def test_detail_partial_sales_no_reactivate(
    db_session: Session,
    test_company: Company,
    sales_user: "User",
):
    """GET company_detail_partial as sales rep: Reactivate button must NOT appear."""
    test_company.is_active = False
    # Give sales_user ownership so they can view it
    test_company.account_owner_id = sales_user.id
    db_session.commit()

    c = _make_client(db_session, sales_user)
    try:
        resp = c.get(f"/v2/partials/customers/{test_company.id}")
    finally:
        _cleanup_overrides()

    assert resp.status_code == 200
    assert "Reactivate" not in resp.text, "Sales rep should NOT see Reactivate button"


# ── 15. L2 — reactivate from archived view returns archived_list partial ─


def test_reactivate_from_archived_returns_archived_list(
    db_session: Session,
    test_company: Company,
    manager_user: "User",
):
    """POST reactivate?from_archived=true → returns archived_list partial (not
    detail)."""
    test_company.is_active = False
    db_session.commit()

    c = _make_client(db_session, manager_user)
    try:
        resp = c.post(f"/v2/partials/customers/{test_company.id}/reactivate?from_archived=true")
    finally:
        _cleanup_overrides()

    assert resp.status_code == 200
    # archived_list has a distinctive heading
    assert "Archived Accounts" in resp.text, "reactivate?from_archived=true should return the archived_list partial"
    # The reactivated company should NOT be in the list
    assert "Acme Electronics" not in resp.text, "Reactivated company should not appear in archived list"


def test_reactivate_from_detail_returns_detail(
    db_session: Session,
    test_company: Company,
    manager_user: "User",
):
    """POST reactivate (no from_archived param) → returns company_detail_partial."""
    test_company.is_active = False
    db_session.commit()

    c = _make_client(db_session, manager_user)
    try:
        resp = c.post(f"/v2/partials/customers/{test_company.id}/reactivate")
    finally:
        _cleanup_overrides()

    assert resp.status_code == 200
    # detail partial has the company name in breadcrumb
    assert "Acme Electronics" in resp.text, (
        "reactivate without from_archived should return detail partial showing the company"
    )


# ── UI entry points: browse-archive link + back link ───────────────────


def test_account_list_has_archive_browse_link(
    db_session: Session,
    test_company: Company,
    test_user: User,
):
    """The active account list exposes a link to browse the Archive (DNC) view — the
    user's 'go and look thru the archive' entry point."""
    c = _make_client(db_session, test_user)
    try:
        resp = c.get("/v2/partials/customers/account-list")
    finally:
        _cleanup_overrides()

    assert resp.status_code == 200
    assert 'hx-get="/v2/partials/customers/archived"' in resp.text, (
        "account list should link to the archived browse view"
    )


def test_archived_view_has_back_link_and_wired_reactivate(
    db_session: Session,
    test_company: Company,
    manager_user: "User",
):
    """The archived browse view has a back-to-accounts link and its Reactivate button
    passes from_archived=true (so the list refreshes, not the detail)."""
    test_company.is_active = False
    db_session.commit()

    c = _make_client(db_session, manager_user)
    try:
        resp = c.get("/v2/partials/customers/archived")
    finally:
        _cleanup_overrides()

    assert resp.status_code == 200
    assert 'hx-get="/v2/partials/customers/account-list"' in resp.text, "archived view needs a back-to-accounts link"
    assert "reactivate?from_archived=true" in resp.text, (
        "Reactivate from the archived view must pass from_archived=true"
    )
