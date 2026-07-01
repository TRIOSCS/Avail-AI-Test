"""Company detail + site-contacts must match the contacts-list ownership scoping.

The global contacts list scopes non-managers to accounts they can_manage (owner /
site-owner / collaborator), but company_detail_partial / company_tab / get_site_contacts
showed full contact PII to any logged-in user by id. Per product decision, scope them to
match the list: a non-manager who can't manage the account gets 404; managers/admins and
the account owner get 200.

Called by: pytest
Depends on: app.routers.htmx.companies, app.routers.proactive,
            conftest (client, db_session, test_user, admin_user)
"""

from app.models.crm import Company, CustomerSite


def _company(db, owner_id):
    c = Company(name="ScopeCo", is_active=True, account_owner_id=owner_id)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _site(db, company_id):
    s = CustomerSite(company_id=company_id, site_name="HQ", is_active=True)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def test_company_detail_404_for_non_manager_non_owner(client, db_session, test_user, admin_user):
    co = _company(db_session, owner_id=admin_user.id)  # owned by someone else
    assert client.get(f"/v2/partials/customers/{co.id}").status_code == 404


def test_company_detail_200_for_account_owner(client, db_session, test_user):
    co = _company(db_session, owner_id=test_user.id)
    assert client.get(f"/v2/partials/customers/{co.id}").status_code == 200


def test_company_tab_404_for_non_manager_non_owner(client, db_session, test_user, admin_user):
    co = _company(db_session, owner_id=admin_user.id)
    assert client.get(f"/v2/partials/customers/{co.id}/tab/contacts").status_code == 404


def test_site_contacts_404_for_non_manager_non_owner(client, db_session, test_user, admin_user):
    co = _company(db_session, owner_id=admin_user.id)
    site = _site(db_session, co.id)
    assert client.get(f"/api/proactive/contacts/{site.id}").status_code == 404


def test_site_contacts_200_for_account_owner(client, db_session, test_user):
    co = _company(db_session, owner_id=test_user.id)
    site = _site(db_session, co.id)
    assert client.get(f"/api/proactive/contacts/{site.id}").status_code == 200
