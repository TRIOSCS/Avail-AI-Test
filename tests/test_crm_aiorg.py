"""test_crm_aiorg.py — Increment 3: AI company organization (durable foundation +
surfaces).

Covers:
- Company.normalized_name derived/backfilled from name (suffix-stripped) + set on create.
- merge_companies appends the loser's name to keep.alternate_names (dedup) + backfills
  keep.normalized_name.
- find_company_dedup_candidates nested shape + finds an obvious near-dup pair on SQLite.
- The settings/data_ops Company-Duplicates review queue renders names (not blank) +
  emits non-empty merge ids for a seeded dup pair (admin client).
- The per-account dup-suggestion route (200 with a Merge affordance when a near-dup
  exists; empty when none; auth-gated).
- The name-suggestion chip surfaces a suggestion for a suffix-heavy name + the apply
  route updates the name.

Called by: pytest
Depends on: conftest.py fixtures, app.company_utils, app.services.company_merge_service,
            app.routers.htmx_views
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.company_utils import find_company_dedup_candidates, normalize_company_name
from app.models import Company, CustomerSite, User
from app.services.company_merge_service import merge_companies


def _make_company(db: Session, name: str, *, sites: int = 0, normalized_name=..., domain=None) -> Company:
    kwargs = dict(
        name=name,
        is_active=True,
        domain=domain,
        created_at=datetime.now(timezone.utc),
    )
    if normalized_name is not ...:
        kwargs["normalized_name"] = normalized_name
    c = Company(**kwargs)
    db.add(c)
    db.flush()
    for i in range(sites):
        db.add(CustomerSite(company_id=c.id, site_name=f"Site {i + 1}", created_at=datetime.now(timezone.utc)))
    db.flush()
    return c


# ── normalized_name on the model ──────────────────────────────────────────


class TestNormalizedName:
    def test_set_explicitly_matches_normalizer(self, db_session: Session):
        c = _make_company(db_session, "Mouser Electronics, Inc.", normalized_name=...)
        # The create paths set normalized_name from the name; emulate the validator/event.
        assert c.normalized_name is not None
        assert c.normalized_name == normalize_company_name("Mouser Electronics, Inc.")
        assert c.normalized_name == "mouser electronics"

    def test_event_fires_on_rename(self, db_session: Session):
        c = _make_company(db_session, "Acme Corp")
        assert c.normalized_name == "acme"
        c.name = "Globex LLC"
        db_session.flush()
        assert c.normalized_name == "globex"

    def test_alternate_names_defaults_to_list(self, db_session: Session):
        c = _make_company(db_session, "Acme Corp")
        db_session.commit()
        db_session.refresh(c)
        assert c.alternate_names == []


# ── merge_companies alternate-name + normalized_name behavior ───────────────


class TestMergeAlternateNames:
    def test_loser_name_appended_to_keep_alternates(self, db_session: Session):
        keep = _make_company(db_session, "Arrow Electronics")
        remove = _make_company(db_session, "Arrow Electronic")
        db_session.commit()

        merge_companies(keep.id, remove.id, db_session)
        db_session.commit()
        db_session.refresh(keep)

        assert "Arrow Electronic" in (keep.alternate_names or [])

    def test_loser_alternates_carried_over_dedup(self, db_session: Session):
        keep = _make_company(db_session, "Arrow Electronics")
        remove = _make_company(db_session, "Arrow Electronic")
        remove.alternate_names = ["Arrow Elec Co", "Arrow Electronics"]  # one dupes keep.name
        db_session.commit()

        merge_companies(keep.id, remove.id, db_session)
        db_session.commit()
        db_session.refresh(keep)

        alts = keep.alternate_names or []
        # Loser name + its alternate are present.
        assert "Arrow Electronic" in alts
        assert "Arrow Elec Co" in alts
        # No duplicates, and keep's own display name is not stored as an alternate.
        assert len(alts) == len(set(alts))
        assert "Arrow Electronics" not in alts

    def test_backfills_keep_normalized_name_when_empty(self, db_session: Session):
        keep = _make_company(db_session, "Arrow Electronics", normalized_name=None)
        remove = _make_company(db_session, "Arrow Electronic")
        db_session.commit()

        merge_companies(keep.id, remove.id, db_session)
        db_session.commit()
        db_session.refresh(keep)

        assert keep.normalized_name == normalize_company_name("Arrow Electronics")


# ── find_company_dedup_candidates (nested shape, SQLite rapidfuzz) ──────────


class TestFindCompanyDedupCandidates:
    def test_nested_shape_and_near_dup(self, db_session: Session):
        a = _make_company(db_session, "Beta Corporation")
        b = _make_company(db_session, "Beta Corp")
        db_session.commit()

        candidates = find_company_dedup_candidates(db_session, threshold=80)
        assert len(candidates) >= 1
        pair = candidates[0]
        # Nested shape preserved.
        assert "company_a" in pair and "company_b" in pair
        assert {"id", "name", "site_count", "has_owner"} <= set(pair["company_a"].keys())
        assert "score" in pair and "auto_keep_id" in pair
        names = {pair["company_a"]["name"], pair["company_b"]["name"]}
        assert names == {"Beta Corporation", "Beta Corp"}
        assert pair["auto_keep_id"] in {a.id, b.id}

    def test_ignores_distinct(self, db_session: Session):
        _make_company(db_session, "Acme Corporation")
        _make_company(db_session, "Zeta Industries")
        db_session.commit()
        assert find_company_dedup_candidates(db_session, threshold=85) == []


# ── settings/data_ops review queue (admin) ─────────────────────────────────


@pytest.fixture()
def admin_client(db_session: Session, admin_user: User) -> TestClient:
    """TestClient whose require_user resolves to an admin (data-ops gates on
    is_admin)."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: admin_user
    app.dependency_overrides[require_admin] = lambda: admin_user
    app.dependency_overrides[require_buyer] = lambda: admin_user

    async def _fresh():
        return "mock-token"

    app.dependency_overrides[require_fresh_token] = _fresh
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in (get_db, require_user, require_admin, require_buyer, require_fresh_token):
            app.dependency_overrides.pop(dep, None)


class TestDataOpsReviewQueue:
    def test_renders_names_and_merge_ids(self, admin_client: TestClient, db_session: Session):
        a = _make_company(db_session, "Beta Corporation", sites=3)
        b = _make_company(db_session, "Beta Corp", sites=1)
        db_session.commit()

        resp = admin_client.get("/v2/partials/settings/data-ops", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        body = resp.text
        # Names render (the old FLAT-key bug rendered blank).
        assert "Beta Corporation" in body
        assert "Beta Corp" in body
        # Non-empty merge ids are emitted (the old bug emitted empty ids).
        assert f'"keep_id": {a.id}' in body or f'"keep_id": {b.id}' in body
        assert "/v2/partials/admin/company-merge" in body

    def test_non_admin_blocked(self, client: TestClient):
        # The default `client` fixture resolves require_user to a buyer.
        resp = client.get("/v2/partials/settings/data-ops", headers={"HX-Request": "true"})
        assert resp.status_code == 403


# ── per-account dup-suggestion banner ──────────────────────────────────────


class TestDupSuggestionRoute:
    def test_returns_merge_affordance_when_near_dup(self, client: TestClient, db_session: Session):
        keep = _make_company(db_session, "Beta Corporation", sites=2)
        _make_company(db_session, "Beta Corp", sites=1)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{keep.id}/dup-suggestion", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "Beta Corp" in resp.text
        # Reuses the merge-form flow.
        assert "/merge-form" in resp.text or "merge-preview" in resp.text

    def test_empty_when_no_dup(self, client: TestClient, db_session: Session):
        solo = _make_company(db_session, "Singular Unique Industries")
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{solo.id}/dup-suggestion", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert resp.text.strip() == ""

    def test_auth_gated(self, unauthenticated_client: TestClient, db_session: Session):
        solo = _make_company(db_session, "Some Company")
        db_session.commit()
        resp = unauthenticated_client.get(f"/v2/partials/customers/{solo.id}/dup-suggestion")
        assert resp.status_code == 401


# ── name-suggestion chip (suggest-only) ────────────────────────────────────


class TestNameSuggestionChip:
    def test_surfaces_suggestion_for_suffix_heavy_name(self, client: TestClient, db_session: Session):
        # "Inc." is stripped but the rest of the (multi-word, display-cased) name is kept.
        c = _make_company(db_session, "Mouser Electronics, Inc.")
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{c.id}/name-suggestion", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        # Suggested name strips the legal suffix while preserving display casing.
        assert "Mouser Electronics" in resp.text
        assert f"/v2/partials/customers/{c.id}/apply-name" in resp.text

    def test_empty_when_name_already_clean(self, client: TestClient, db_session: Session):
        c = _make_company(db_session, "Phoenix Trading")
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{c.id}/name-suggestion", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert resp.text.strip() == ""

    def test_apply_route_updates_name(self, client: TestClient, db_session: Session):
        c = _make_company(db_session, "Mouser Electronics, Inc.")
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{c.id}/apply-name",
            data={"name": "Mouser Electronics"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.expire(c)
        refreshed = db_session.get(Company, c.id)
        assert refreshed.name == "Mouser Electronics"
        # normalized_name tracks the new name.
        assert refreshed.normalized_name == normalize_company_name("Mouser Electronics")


class TestCreateIsSuggestOnly:
    """create_company must NOT silently rewrite the rep's typed name (suggest-only)."""

    def test_create_keeps_typed_name(self, client: TestClient, db_session: Session):
        from unittest.mock import AsyncMock, patch

        # AI typo-fix would "correct" the typed name; suggest-only means it is NOT stored.
        with (
            patch("app.routers.crm.companies.get_credential_cached", return_value=None),
            patch("app.enrichment_service.normalize_company_input", new_callable=AsyncMock) as mock_norm,
        ):
            mock_norm.return_value = ("Corrected Name", "typedco.com")
            resp = client.post("/api/companies", json={"name": "Typedd Naame"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Typedd Naame"  # what the rep typed, NOT the AI rewrite
        stored = db_session.get(Company, data["id"])
        # normalized_name is derived from the stored (typed) name.
        assert stored.normalized_name == normalize_company_name("Typedd Naame")
