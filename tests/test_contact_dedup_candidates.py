"""test_contact_dedup_candidates.py — TDD for the cross-company contact dedupe sweep
(survey idea #15).

The nightly _job_contact_dedup already auto-merges same-SITE + same-email(case) contacts
(kept, out of scope). This feature closes the documented gap — CROSS-company/cross-site
contact dupes had zero detection — as SUGGEST-ONLY:
  - find_contact_dedup_candidates() surfaces high-precision cross-site pairs:
      band "email"       — identical email, different customer_site (strong)
      band "name+domain" — fuzzy full_name + shared email domain, different site (review)
    (pure name-only cross-company pairs are deliberately NOT surfaced — too noisy; the
     plan's "respect same-name-different-company" risk).
  - pairs render in the Data Ops tab (computed live — NO new table), one-tap Merge via the
    existing contact_merge_service, or Dismiss; contacts NEVER auto-merge here.
  - an on-demand per-pair "AI: same person?" assist is available for the review band.

Called by: pytest (TESTING=1 PYTHONPATH=. pytest tests/test_contact_dedup_candidates.py -v)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.crm import Company, CustomerSite, SiteContact


def _company(db: Session, name: str) -> Company:
    c = Company(name=name)
    db.add(c)
    db.flush()
    return c


def _site(db: Session, company: Company, site_name: str = "HQ") -> CustomerSite:
    s = CustomerSite(company_id=company.id, site_name=site_name)
    db.add(s)
    db.flush()
    return s


def _contact(db: Session, site: CustomerSite, full_name: str, email: str | None, **kw) -> SiteContact:
    c = SiteContact(customer_site_id=site.id, full_name=full_name, email=email, **kw)
    db.add(c)
    db.flush()
    return c


def _two_sites(db: Session):
    """Two distinct companies, each with one site — the cross-company setup."""
    sa = _site(db, _company(db, "Acme Corp"))
    sb = _site(db, _company(db, "Beta LLC"))
    return sa, sb


# ── finder: email-exact band ─────────────────────────────────────────────────────


class TestEmailBand:
    def test_same_email_cross_site_is_a_pair(self, db_session):
        from app.services.contact_dedup_candidates import find_contact_dedup_candidates

        sa, sb = _two_sites(db_session)
        _contact(db_session, sa, "Jane Doe", "jane@acme.com")
        _contact(db_session, sb, "Jane Doe", "JANE@acme.com")  # case variant, different site
        db_session.commit()
        pairs = find_contact_dedup_candidates(db_session)
        assert len(pairs) == 1
        p = pairs[0]
        assert p["match"] == "email"
        assert p["score"] == 100
        assert {p["contact_a"]["company_name"], p["contact_b"]["company_name"]} == {"Acme Corp", "Beta LLC"}

    def test_same_email_same_site_is_not_a_pair(self, db_session):
        """Same-site same-email is the existing nightly job's job — never surfaced
        here."""
        from app.services.contact_dedup_candidates import find_contact_dedup_candidates

        sa = _site(db_session, _company(db_session, "Acme Corp"))
        _contact(db_session, sa, "Jane Doe", "jane@acme.com")
        _contact(db_session, sa, "Jane D", "JANE@acme.com")  # case variant the case-sensitive uq permits
        db_session.commit()
        assert find_contact_dedup_candidates(db_session) == []

    def test_empty_or_null_email_ignored(self, db_session):
        from app.services.contact_dedup_candidates import find_contact_dedup_candidates

        sa, sb = _two_sites(db_session)
        _contact(db_session, sa, "No Email", "")
        _contact(db_session, sb, "No Email", None)
        db_session.commit()
        assert find_contact_dedup_candidates(db_session) == []

    def test_archived_contacts_excluded(self, db_session):
        from app.services.contact_dedup_candidates import find_contact_dedup_candidates

        sa, sb = _two_sites(db_session)
        _contact(db_session, sa, "Jane Doe", "jane@acme.com")
        _contact(db_session, sb, "Jane Doe", "jane@acme.com", is_archived=True)
        db_session.commit()
        assert find_contact_dedup_candidates(db_session) == []

    def test_auto_keep_id_prefers_more_complete_contact(self, db_session):
        from app.services.contact_dedup_candidates import find_contact_dedup_candidates

        sa, sb = _two_sites(db_session)
        sparse = _contact(db_session, sa, "Jane Doe", "jane@acme.com")
        rich = _contact(
            db_session,
            sb,
            "Jane Doe",
            "jane@acme.com",
            title="Buyer",
            phone="555-1212",
            linkedin_url="x",
            contact_role="buyer",
        )
        db_session.commit()
        p = find_contact_dedup_candidates(db_session)[0]
        assert p["auto_keep_id"] == rich.id  # richer record kept, not the sparse one
        assert sparse.id != rich.id


# ── finder: name+domain band ─────────────────────────────────────────────────────


class TestNameDomainBand:
    def test_fuzzy_name_shared_domain_cross_site_is_a_pair(self, db_session):
        from app.services.contact_dedup_candidates import find_contact_dedup_candidates

        sa, sb = _two_sites(db_session)
        _contact(db_session, sa, "Jonathan Smith", "j.smith@acme.com")
        _contact(db_session, sb, "Jonathon Smith", "jonathon.smith@acme.com")  # spelling variant, same domain
        db_session.commit()
        pairs = find_contact_dedup_candidates(db_session)
        assert len(pairs) == 1
        assert pairs[0]["match"] == "name+domain"
        assert pairs[0]["score"] >= 82

    def test_similar_name_different_domain_not_surfaced(self, db_session):
        """Name-only cross-company (no shared email/domain) is too noisy — excluded."""
        from app.services.contact_dedup_candidates import find_contact_dedup_candidates

        sa, sb = _two_sites(db_session)
        _contact(db_session, sa, "John Smith", "john@acme.com")
        _contact(db_session, sb, "John Smith", "john@beta.com")  # different domain
        db_session.commit()
        assert find_contact_dedup_candidates(db_session) == []

    def test_shared_domain_unrelated_names_not_surfaced(self, db_session):
        from app.services.contact_dedup_candidates import find_contact_dedup_candidates

        sa, sb = _two_sites(db_session)
        _contact(db_session, sa, "Alice Wong", "alice@acme.com")
        _contact(db_session, sb, "Bob Vance", "bob@acme.com")  # same domain, unrelated names
        db_session.commit()
        assert find_contact_dedup_candidates(db_session) == []

    def test_public_email_domain_excluded_from_name_band(self, db_session):
        """gmail.com et al.

        carry no 'same org' signal — similar names there are not a pair.
        """
        from app.services.contact_dedup_candidates import find_contact_dedup_candidates

        sa, sb = _two_sites(db_session)
        _contact(db_session, sa, "Jonathan Smith", "jonathan.smith@gmail.com")
        _contact(db_session, sb, "Jonathon Smith", "jonathon.smith@gmail.com")  # public domain
        db_session.commit()
        assert find_contact_dedup_candidates(db_session) == []


class TestGuards:
    def test_generic_shared_inbox_fully_suppressed(self, db_session):
        """A single email shared by many contacts is a generic inbox — suppressed in
        BOTH bands (the oversized email must not leak back through the shared-domain
        band)."""
        from app.services.contact_dedup_candidates import find_contact_dedup_candidates

        # 8 sites, each a contact at the SAME shared inbox address → group size 8 > cap.
        for i in range(8):
            s = _site(db_session, _company(db_session, f"Co{i}"))
            _contact(db_session, s, f"Person {i}", "info@vendor.com")
        db_session.commit()
        # Not one email pair AND not one name+domain pair — the shared inbox is fully skipped.
        assert find_contact_dedup_candidates(db_session) == []

    def test_trailing_at_empty_domain_not_grouped(self, db_session):
        """A malformed 'foo@' address has an empty domain — it must not bucket unrelated
        contacts together in the name+domain band."""
        from app.services.contact_dedup_candidates import find_contact_dedup_candidates

        sa, sb = _two_sites(db_session)
        _contact(db_session, sa, "Jonathan Smith", "jonathan@")  # empty domain
        _contact(db_session, sb, "Jonathon Smith", "jonathon@")  # empty domain, similar name
        db_session.commit()
        assert find_contact_dedup_candidates(db_session) == []


# ── Data Ops surface + merge + AI-check routes ───────────────────────────────────


@pytest.fixture()
def admin_client(db_session, admin_user):
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: admin_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in (get_db, require_user):
            app.dependency_overrides.pop(dep, None)


def _email_pair(db):
    sa, sb = _two_sites(db)
    a = _contact(db, sa, "Jane Doe", "jane@acme.com")
    b = _contact(db, sb, "Jane Doe", "JANE@acme.com")
    db.commit()
    return a, b


class TestDataOpsRoutes:
    def test_data_ops_renders_contacts_section(self, admin_client, db_session):
        _email_pair(db_session)
        resp = admin_client.get("/v2/partials/settings/data-ops", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "Contact Duplicates" in resp.text
        assert "Acme Corp" in resp.text and "Beta LLC" in resp.text
        assert "same email" in resp.text

    def test_admin_contact_merge_merges_and_rerenders(self, admin_client, db_session):
        from app.models.crm import SiteContact

        a, b = _email_pair(db_session)
        resp = admin_client.post(
            "/v2/partials/admin/contact-merge",
            data={"keep_id": a.id, "remove_id": b.id},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # loser deleted, keeper survives → the pair is gone from the re-rendered pane
        assert db_session.get(SiteContact, b.id) is None
        assert db_session.get(SiteContact, a.id) is not None
        assert "No cross-company contact duplicates found." in resp.text

    def test_contact_merge_non_admin_403(self, client, db_session):
        a, b = _email_pair(db_session)
        resp = client.post(
            "/v2/partials/admin/contact-merge",
            data={"keep_id": a.id, "remove_id": b.id},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 403

    def test_ai_check_renders_verdict(self, admin_client, db_session):
        a, b = _email_pair(db_session)
        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            return_value={"same_person": True, "confidence": 0.9, "reason": "identical email"},
        ):
            resp = admin_client.post(
                "/v2/partials/admin/contact-ai-check",
                data={"id_a": a.id, "id_b": b.id},
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200
        assert "Likely same person" in resp.text
        assert "90%" in resp.text

    def test_ai_check_different_people_verdict(self, admin_client, db_session):
        a, b = _email_pair(db_session)
        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            return_value={"same_person": False, "confidence": 0.8, "reason": "different companies, no other match"},
        ):
            resp = admin_client.post(
                "/v2/partials/admin/contact-ai-check",
                data={"id_a": a.id, "id_b": b.id},
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200
        assert "Likely different people" in resp.text
        assert "80%" in resp.text

    def test_ai_check_graceful_on_failure(self, admin_client, db_session):
        """An AI error renders an advisory chip, never a 500."""
        from app.utils.claude_errors import ClaudeError

        a, b = _email_pair(db_session)
        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            side_effect=ClaudeError("boom"),
        ):
            resp = admin_client.post(
                "/v2/partials/admin/contact-ai-check",
                data={"id_a": a.id, "id_b": b.id},
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200
        assert "reach the AI just now" in resp.text  # graceful chip (apostrophe is HTML-escaped)

    def test_ai_check_non_admin_403(self, client, db_session):
        a, b = _email_pair(db_session)
        resp = client.post(
            "/v2/partials/admin/contact-ai-check",
            data={"id_a": a.id, "id_b": b.id},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 403

    def test_dismissed_contact_pair_excluded_from_render(self, admin_client, db_session):
        """A persisted contact dismissal removes the pair from the candidates list."""
        from app.models import DedupDecision

        a, b = _email_pair(db_session)
        lo, hi = sorted((a.id, b.id))
        db_session.add(DedupDecision(entity_type="contact", id_a=lo, id_b=hi))
        db_session.commit()
        resp = admin_client.get("/v2/partials/settings/data-ops", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "No cross-company contact duplicates found." in resp.text
