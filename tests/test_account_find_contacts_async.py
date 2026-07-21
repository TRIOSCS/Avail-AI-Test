"""test_account_find_contacts_async.py — the CRM account Contacts-tab "Find contacts"
button must return instantly.

The bug: GET /v2/partials/customers/{id}/suggested-contacts ran the multi-provider
contact-discovery waterfall INLINE — find_suggested_contacts_with_errors (Hunter/Clay/
Lusha/Explorium, ~10-40s) — before responding, so the "Find contacts" click felt hung.
The fix schedules that heavy work as a FastAPI BackgroundTask and returns a "Finding
contacts…" poller immediately; the status poller then swaps in the discovered-contacts
panel (or the amber "couldn't reach" degraded banner) once the background run finishes.

A SEPARATE registry (app.services.contact_discovery_runs) is used on purpose — the header
"Enrich" button uses company_enrich_runs, and sharing a key would make the two buttons
block each other on the same company.

Covers:
  * HTMX endpoint returns immediately — the waterfall is scheduled, NOT awaited inline;
  * the returned partial shows the polling "Finding contacts…" panel + sets in-flight state;
  * double-enqueue is guarded (a run already in flight does not stack another);
  * the background runner records the run outcome (contacts / degraded providers), opens
    NO DB session (pure external call), and never raises;
  * the poller reflects in-progress (keep polling) then terminal (render the result panel +
    stop polling at 286), and surfaces the amber "couldn't reach" banner on degraded
    providers.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user),
            app.routers.htmx.companies, app.services.contact_discovery_runs.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import BackgroundTasks
from starlette.requests import Request

from app.services.contact_discovery_runs import ContactDiscoveryOutcome, contact_discovery_runs


@pytest.fixture(autouse=True)
def _clear_contact_runs():
    """Reset the process-wide in-flight registry around every test (isolation)."""
    contact_discovery_runs._state.clear()
    yield
    contact_discovery_runs._state.clear()


def _get_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"hx-request", b"true")],
            "query_string": b"",
        }
    )


def _make_company(db_session, test_user, **kw):
    from app.models import Company

    defaults = {"name": "Find Co", "domain": "find.com", "is_active": True, "account_owner_id": test_user.id}
    defaults.update(kw)
    company = Company(**defaults)
    db_session.add(company)
    db_session.commit()
    db_session.refresh(company)
    return company


# ── Endpoint (HTMX): returns immediately, schedules the waterfall ────────────────────


@pytest.mark.asyncio
async def test_find_contacts_schedules_background_and_does_not_run_inline(db_session, test_user, monkeypatch):
    """The handler registers a background task and returns WITHOUT awaiting the
    waterfall.

    Proven by: find_suggested_contacts_with_errors is not called during the handler, a task
    IS queued on the BackgroundTasks object, and the account is left in the in-flight state
    with the polling panel returned.
    """
    from app.routers.htmx import companies as comp

    find_mock = AsyncMock(return_value=([], []))
    monkeypatch.setattr("app.enrichment_service.find_suggested_contacts_with_errors", find_mock)
    # Neutralise the runner so this test only inspects scheduling (no real providers hit).
    monkeypatch.setattr(comp, "_run_contact_discovery", AsyncMock())

    company = _make_company(db_session, test_user, name="Async Co", domain="async.com")

    bg = BackgroundTasks()
    resp = await comp.contacts_tab_suggested(_get_request(), company.id, bg, "async.com", test_user, db_session)

    find_mock.assert_not_called()  # waterfall NOT awaited inline
    assert len(bg.tasks) == 1  # a background task WAS registered
    assert contact_discovery_runs.is_running(company.id) is True
    body = resp.body.decode()
    assert "Finding contacts" in body
    assert "every 2s" in body


def test_find_contacts_returns_poller_over_http(client, db_session, test_user, monkeypatch):
    """Via HTTP: the button GET returns the polling panel and points at the status route."""
    from app.routers.htmx import companies as comp

    find_mock = AsyncMock(return_value=([], []))
    monkeypatch.setattr("app.enrichment_service.find_suggested_contacts_with_errors", find_mock)
    monkeypatch.setattr(comp, "_run_contact_discovery", AsyncMock())

    company = _make_company(db_session, test_user, name="Panel Co", domain="panel.com")

    resp = client.get(f"/v2/partials/customers/{company.id}/suggested-contacts", params={"domain": "panel.com"})
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert "Finding contacts" in resp.text
    assert "every 2s" in resp.text  # poller active
    assert f"/v2/partials/customers/{company.id}/suggested-contacts/status" in resp.text
    find_mock.assert_not_called()  # scheduled, not awaited inline
    assert contact_discovery_runs.is_running(company.id) is True


def test_find_contacts_double_click_does_not_stack_second_run(client, db_session, test_user, monkeypatch):
    """A run already in flight must not enqueue a second background waterfall."""
    from app.routers.htmx import companies as comp

    runner = AsyncMock()
    monkeypatch.setattr(comp, "_run_contact_discovery", runner)

    company = _make_company(db_session, test_user, name="Dbl Co", domain="dbl.com")

    # Simulate a run already in flight.
    assert contact_discovery_runs.begin(company.id) is True

    resp = client.get(f"/v2/partials/customers/{company.id}/suggested-contacts", params={"domain": "dbl.com"})
    assert resp.status_code == 200
    assert "Finding contacts" in resp.text
    runner.assert_not_called()  # no second run scheduled


def test_find_contacts_missing_domain_400(client, db_session, test_user):
    """No domain on the company and none in the query → 400 (nothing to discover)."""
    company = _make_company(db_session, test_user, name="NoDomain Co", domain=None, website=None)
    resp = client.get(f"/v2/partials/customers/{company.id}/suggested-contacts")
    assert resp.status_code == 400


# ── Background runner: outcome recorded, no DB session, never raises ──────────────────


@pytest.mark.asyncio
async def test_background_run_records_contacts_outcome(monkeypatch):
    """Runner records the discovered contacts and opens NO DB session (pure external
    call)."""
    from app.routers.htmx.companies import _run_contact_discovery

    session_factory = MagicMock()
    monkeypatch.setattr("app.database.SessionLocal", session_factory)
    contact = {"full_name": "Jane Buyer", "title": "Buyer", "email": "jane@acme.com", "source": "hunter"}
    monkeypatch.setattr(
        "app.enrichment_service.find_suggested_contacts_with_errors",
        AsyncMock(return_value=([contact], [])),
    )

    contact_discovery_runs.begin(5151)
    await _run_contact_discovery(5151, "acme.com", "Acme")

    outcome = contact_discovery_runs.consume_outcome(5151)
    assert outcome is not None
    assert outcome.suggested == [contact]
    assert outcome.errored_providers == []
    session_factory.assert_not_called()  # contact discovery never touches the DB


@pytest.mark.asyncio
async def test_background_run_degraded_provider_preserved(monkeypatch):
    """A degraded provider (returned in errored list) is carried through to the
    outcome."""
    from app.routers.htmx.companies import _run_contact_discovery

    monkeypatch.setattr(
        "app.enrichment_service.find_suggested_contacts_with_errors",
        AsyncMock(return_value=([], ["hunter"])),
    )

    contact_discovery_runs.begin(5152)
    await _run_contact_discovery(5152, "acme.com", "Acme")

    outcome = contact_discovery_runs.consume_outcome(5152)
    assert outcome is not None
    assert outcome.suggested == []
    assert outcome.errored_providers == ["hunter"]


@pytest.mark.asyncio
async def test_background_run_exception_degrades_all_never_raises(monkeypatch):
    """The waterfall raising (connectivity/unexpected) → errored=['all'], never
    raises."""
    from app.routers.htmx.companies import _run_contact_discovery

    monkeypatch.setattr(
        "app.enrichment_service.find_suggested_contacts_with_errors",
        AsyncMock(side_effect=RuntimeError("clay down")),
    )

    contact_discovery_runs.begin(5153)
    await _run_contact_discovery(5153, "acme.com", "Acme")  # must not raise

    outcome = contact_discovery_runs.consume_outcome(5153)
    assert outcome is not None
    assert outcome.suggested == []
    assert outcome.errored_providers == ["all"]


# ── Poller: in-progress keeps polling, terminal renders panel, degraded banner ───────


def test_poller_in_progress_keeps_polling(client, db_session, test_user):
    company = _make_company(db_session, test_user, name="Poll Co", domain="poll.com")
    contact_discovery_runs.begin(company.id)  # running, no outcome yet

    resp = client.get(f"/v2/partials/customers/{company.id}/suggested-contacts/status")
    assert resp.status_code == 200
    assert "Finding contacts" in resp.text
    assert "every 2s" in resp.text


def test_poller_terminal_renders_contacts_and_stops(client, db_session, test_user):
    """On completion the poller returns the contacts panel and stops polling (286); the
    outcome is consumed exactly once."""
    company = _make_company(db_session, test_user, name="Acme Electronics", domain="acme.com")
    contact = {"full_name": "Jane Buyer", "title": "Procurement Mgr", "email": "jane@acme.com", "source": "hunter"}
    contact_discovery_runs.finish(
        company.id,
        ContactDiscoveryOutcome(suggested=[contact], errored_providers=[]),
    )

    resp = client.get(f"/v2/partials/customers/{company.id}/suggested-contacts/status")
    assert resp.status_code == 286  # htmx stop-polling
    assert "Jane Buyer" in resp.text  # discovered contact
    assert "suggested-contacts/add" in resp.text  # per-row Add form
    assert "every 2s" not in resp.text  # no longer polling
    # Outcome consumed once.
    assert contact_discovery_runs.consume_outcome(company.id) is None


def test_poller_terminal_neutral_empty_state(client, db_session, test_user):
    """Zero results + no errors → neutral 'No contacts found', 286, no amber banner."""
    company = _make_company(db_session, test_user, name="Empty Co", domain="empty.com")
    contact_discovery_runs.finish(company.id, ContactDiscoveryOutcome(suggested=[], errored_providers=[]))

    resp = client.get(f"/v2/partials/customers/{company.id}/suggested-contacts/status")
    assert resp.status_code == 286
    assert "No contacts found" in resp.text
    assert "Couldn" not in resp.text  # no error banner


def test_poller_degraded_providers_amber_banner(client, db_session, test_user):
    """Degraded providers → the amber 'Couldn't reach <provider>' banner (mirrors the
    old inline behavior)."""
    company = _make_company(db_session, test_user, name="Amber Co", domain="amber.com")
    contact_discovery_runs.finish(company.id, ContactDiscoveryOutcome(suggested=[], errored_providers=["hunter"]))

    resp = client.get(f"/v2/partials/customers/{company.id}/suggested-contacts/status")
    assert resp.status_code == 286
    assert "hunter" in resp.text.lower()
    assert "couldn" in resp.text.lower()  # amber "Couldn't reach" banner


def test_poller_no_outcome_stops_polling(client, db_session, test_user):
    """No run in flight and no pending outcome → stop polling with an empty body."""
    company = _make_company(db_session, test_user, name="Gone Co", domain="gone.com")
    resp = client.get(f"/v2/partials/customers/{company.id}/suggested-contacts/status")
    assert resp.status_code == 286
    assert resp.text.strip() == ""


def test_poller_deleted_company_stops_polling(client):
    resp = client.get("/v2/partials/customers/999999/suggested-contacts/status")
    assert resp.status_code == 286
    assert resp.text.strip() == ""


# ── ISS-025: poller drops suggestions already on file for this company ────────────────


def _make_site_contact(db_session, company, *, full_name, email=None, site_name="HQ"):
    from app.models import CustomerSite, SiteContact

    site = CustomerSite(company_id=company.id, site_name=site_name, is_active=True)
    db_session.add(site)
    db_session.flush()
    contact = SiteContact(customer_site_id=site.id, full_name=full_name, email=email, is_active=True)
    db_session.add(contact)
    db_session.commit()
    return contact


def test_poller_drops_suggestion_matching_existing_contact_email(client, db_session, test_user):
    """A suggestion whose email matches an existing SiteContact (case-insensitively) is
    dropped from the rendered panel — it never even round-trips through the "Add"
    button."""
    company = _make_company(db_session, test_user, name="Dedup Co", domain="dedup.com")
    _make_site_contact(db_session, company, full_name="Jane Buyer", email="Jane@Dedup.com")

    dup = {"full_name": "Jane B.", "email": "jane@dedup.com", "source": "hunter"}
    new = {"full_name": "New Person", "email": "new@dedup.com", "source": "hunter"}
    contact_discovery_runs.finish(company.id, ContactDiscoveryOutcome(suggested=[dup, new], errored_providers=[]))

    resp = client.get(f"/v2/partials/customers/{company.id}/suggested-contacts/status")
    assert resp.status_code == 286
    assert "Jane B." not in resp.text
    assert "New Person" in resp.text


def test_poller_drops_suggestion_matching_existing_contact_name_when_no_email(client, db_session, test_user):
    """A suggestion with NO email falls back to a normalized full-name match against an
    existing contact."""
    company = _make_company(db_session, test_user, name="NameDedup Co", domain="namededup.com")
    _make_site_contact(db_session, company, full_name="  Dana   Wu ")

    dup = {"full_name": "dana wu", "source": "hunter"}  # no email
    new = {"full_name": "Fresh Contact", "source": "hunter"}
    contact_discovery_runs.finish(company.id, ContactDiscoveryOutcome(suggested=[dup, new], errored_providers=[]))

    resp = client.get(f"/v2/partials/customers/{company.id}/suggested-contacts/status")
    assert resp.status_code == 286
    assert "dana wu" not in resp.text.lower()
    assert "Fresh Contact" in resp.text


def test_poller_all_suggestions_deduped_renders_none_found(client, db_session, test_user):
    """Every suggestion matches an existing contact -> the panel renders the neutral
    'No contacts found' state, not stale duplicates."""
    company = _make_company(db_session, test_user, name="AllDup Co", domain="alldup.com")
    _make_site_contact(db_session, company, full_name="Solo Contact", email="solo@alldup.com")

    dup = {"full_name": "Solo Contact", "email": "solo@alldup.com", "source": "hunter"}
    contact_discovery_runs.finish(company.id, ContactDiscoveryOutcome(suggested=[dup], errored_providers=[]))

    resp = client.get(f"/v2/partials/customers/{company.id}/suggested-contacts/status")
    assert resp.status_code == 286
    assert "No contacts found" in resp.text


# ── End-to-end: button GET runs the (mocked) waterfall in the bg task, status swaps ──


def test_full_flow_button_then_status_renders_results(client, db_session, test_user, monkeypatch):
    """Button GET schedules the waterfall (TestClient runs it in the bg task); a follow-
    up status GET then renders the discovered contacts and stops polling."""
    contact = {"full_name": "Bob Buyer", "title": "VP Procurement", "email": "bob@acme.com", "source": "hunter"}
    monkeypatch.setattr(
        "app.enrichment_service.find_suggested_contacts_with_errors",
        AsyncMock(return_value=([contact], [])),
    )
    company = _make_company(db_session, test_user, name="E2E Co", domain="e2e.com")

    # Button GET returns the poller AND (via TestClient) runs the bg discovery task.
    first = client.get(f"/v2/partials/customers/{company.id}/suggested-contacts", params={"domain": "e2e.com"})
    assert first.status_code == 200
    assert "Finding contacts" in first.text

    # The bg task has stored the outcome; the status poll now renders the results.
    second = client.get(f"/v2/partials/customers/{company.id}/suggested-contacts/status")
    assert second.status_code == 286
    assert "Bob Buyer" in second.text
    assert "suggested-contacts/add" in second.text
