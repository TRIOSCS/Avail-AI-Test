"""tests/test_customer360_siblings.py — Customer-360 sibling pooling (AI queue P3-7).

Covers: company_utils.find_sibling_companies, sibling pooling in
account_summary_service.generate_account_summary, and the account-summary
HTMX partials.
"""

from unittest.mock import AsyncMock, patch

from app.company_utils import find_sibling_companies
from app.models.crm import Company, CustomerSite
from app.services.account_summary_service import generate_account_summary


def _mk_company(db, name, *, owner_id=None, is_active=True):
    c = Company(name=name, account_owner_id=owner_id, is_active=is_active)
    db.add(c)
    db.commit()
    return c


_AI = {"situation": "s", "development": "d", "next_steps": ["n1"]}


def _run(coro):
    import asyncio

    return asyncio.get_event_loop().run_until_complete(coro)


class TestFindSiblings:
    def test_same_normalized_name_found(self, db_session, test_company):
        sib = _mk_company(db_session, f"{test_company.name}, Inc.")
        assert sib.normalized_name == test_company.normalized_name  # validator sync
        got = find_sibling_companies(db_session, test_company)
        assert [c.id for c in got] == [sib.id]

    def test_self_and_inactive_and_unrelated_excluded(self, db_session, test_company):
        _mk_company(db_session, f"{test_company.name} LLC", is_active=False)
        _mk_company(db_session, "Totally Different Corp")
        got = find_sibling_companies(db_session, test_company)
        assert got == []

    def test_null_normalized_name_no_siblings(self, db_session, test_company):
        test_company.normalized_name = None
        db_session.commit()
        assert find_sibling_companies(db_session, test_company) == []

    def test_cap(self, db_session, test_company):
        # Guaranteed-identical normalized names (assigned directly):
        sibs = []
        for i in range(7):
            c = _mk_company(db_session, f"{test_company.name} whatever {i}")
            c.normalized_name = test_company.normalized_name
            sibs.append(c)
        db_session.commit()
        got = find_sibling_companies(db_session, test_company, cap=5)
        assert len(got) == 5


class TestSiblingPooling:
    def test_no_siblings_prompt_unchanged_and_empty_list(self, db_session, test_company):
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=_AI) as mock_ai:
            out = _run(generate_account_summary(test_company.id, db_session))
        assert out["sibling_accounts"] == []
        assert "Sibling accounts pooled" not in mock_ai.call_args.args[0]

    def test_sibling_sites_and_activities_pooled(self, db_session, test_company, test_user):
        sib = _mk_company(db_session, "Sibling Co", owner_id=test_user.id)
        sib.normalized_name = test_company.normalized_name
        db_session.add(CustomerSite(company_id=sib.id, site_name="Sib Plant"))
        db_session.commit()
        from app.models.intelligence import ActivityLog

        db_session.add(
            ActivityLog(activity_type="sales_note", channel="manual", company_id=sib.id, subject="sib touchpoint")
        )
        db_session.commit()
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=_AI) as mock_ai:
            out = _run(generate_account_summary(test_company.id, db_session))
        prompt = mock_ai.call_args.args[0]
        assert "Sibling accounts pooled (1): Sibling Co" in prompt
        assert out["sibling_accounts"] == [
            {"id": sib.id, "name": "Sibling Co", "owner": test_user.name or test_user.email}
        ]
        # pooled: the sibling's activity is inside the 20-row activity window
        assert "Recent activity" in prompt

    def test_failure_still_returns_empty_dict(self, db_session, test_company):
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            out = _run(generate_account_summary(test_company.id, db_session))
        assert out == {}


class TestAccountSummaryPanel:
    def test_get_renders_generate_button(self, client, db_session, test_company, test_user):
        test_company.account_owner_id = test_user.id
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{test_company.id}/account-summary", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "Generate" in resp.text
        assert f"/v2/partials/customers/{test_company.id}/account-summary" in resp.text

    def test_post_renders_summary_with_sibling_banner(self, client, db_session, test_company, test_user):
        test_company.account_owner_id = test_user.id
        db_session.commit()
        fake = {
            "situation": "Solid account.",
            "development": "Growing.",
            "next_steps": ["Call the buyer"],
            "sibling_accounts": [{"id": 9, "name": "Sib Co", "owner": "Pat"}],
        }
        with (
            patch(
                "app.services.account_summary_service.generate_account_summary",
                new_callable=AsyncMock,
                return_value=fake,
            ),
            patch("app.routers.htmx.insights_views.check_rate_limit", return_value=True),
        ):
            resp = client.post(
                f"/v2/partials/customers/{test_company.id}/account-summary", headers={"HX-Request": "true"}
            )
        assert resp.status_code == 200
        assert "Solid account." in resp.text
        assert "Includes 1 sibling account" in resp.text
        assert "Sib Co" in resp.text and "Pat" in resp.text

    def test_post_ai_unavailable_friendly(self, client, db_session, test_company, test_user):
        test_company.account_owner_id = test_user.id
        db_session.commit()
        with (
            patch(
                "app.services.account_summary_service.generate_account_summary",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch("app.routers.htmx.insights_views.check_rate_limit", return_value=True),
        ):
            resp = client.post(
                f"/v2/partials/customers/{test_company.id}/account-summary", headers={"HX-Request": "true"}
            )
        assert resp.status_code == 200
        assert "unavailable" in resp.text.lower()

    def test_post_rate_limited_friendly(self, client, db_session, test_company, test_user):
        test_company.account_owner_id = test_user.id
        db_session.commit()
        with patch("app.routers.htmx.insights_views.check_rate_limit", return_value=False):
            resp = client.post(
                f"/v2/partials/customers/{test_company.id}/account-summary", headers={"HX-Request": "true"}
            )
        assert resp.status_code == 200
        assert "wait" in resp.text.lower()

    def test_non_manager_gets_forbidden_card(self, client, db_session, test_company):
        # client's test_user (buyer) neither owns nor manages test_company
        resp = client.get(f"/v2/partials/customers/{test_company.id}/account-summary", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "restricted" in resp.text.lower() or "not available" in resp.text.lower()

    def test_missing_company_404(self, client, db_session):
        resp = client.get("/v2/partials/customers/999999/account-summary", headers={"HX-Request": "true"})
        assert resp.status_code == 404
