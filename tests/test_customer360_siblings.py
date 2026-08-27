"""tests/test_customer360_siblings.py — Customer-360 sibling pooling (AI queue P3-7).

Covers: company_utils.find_sibling_companies, sibling pooling in
account_summary_service.generate_account_summary, and the account-summary
HTMX partials.
"""

from app.company_utils import find_sibling_companies
from app.models.crm import Company


def _mk_company(db, name, *, owner_id=None, is_active=True):
    c = Company(name=name, account_owner_id=owner_id, is_active=is_active)
    db.add(c)
    db.commit()
    return c


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
