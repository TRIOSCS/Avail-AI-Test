"""test_account_enrich_async.py — the CRM account (Company) "Enrich" button must return
instantly.

The bug: POST /api/enrich/company/{id} (HTMX path) ran the full external-provider +
AI + contact-discovery waterfall INLINE — enrich_entity (SAM.gov ~15s + Clay/Explorium/
Lusha + Anthropic) then find_suggested_contacts_with_errors (Hunter/Clay) — before
responding, so the Enrich click felt hung for ~20-40s. The fix schedules that heavy work
as a FastAPI BackgroundTask and returns an "Enriching…" poller immediately; the enrich-
status poller then swaps in the firmographics + discovered-contacts panel (or a "couldn't
complete" toast) once the background run finishes.

Covers:
  * HTMX endpoint returns immediately — the waterfall is scheduled, NOT awaited inline;
  * the returned partial shows the polling "Enriching…" panel + sets the in-flight state;
  * double-enqueue is guarded (a run already in flight does not stack another);
  * the background runner records the run outcome (success / firmographics-blocked /
    contact-discovery-degraded) and opens+closes its own session, never raising;
  * the poller reflects in-progress (keep polling) then terminal (render the result panel
    + stop polling), and surfaces the "couldn't complete" toast on a blocked run;
  * the JSON/programmatic path stays synchronous (unchanged API contract).

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user, test_company),
            app.routers.crm.enrichment, app.services.company_enrich_runs.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import BackgroundTasks
from starlette.requests import Request

from app.schemas.crm import EnrichDomainRequest
from app.services.company_enrich_runs import CompanyEnrichOutcome, company_enrich_runs


@pytest.fixture(autouse=True)
def _clear_company_runs():
    """Reset the process-wide in-flight registry around every test (isolation)."""
    company_enrich_runs._state.clear()
    yield
    company_enrich_runs._state.clear()


@pytest.fixture(autouse=True)
def _provider_configured(monkeypatch):
    """Make _require_enrichment_provider() pass so requests reach the endpoint body."""
    import app.routers.crm.enrichment as enrichment_router

    monkeypatch.setattr(enrichment_router, "get_credential_cached", lambda *a, **k: "TEST_KEY")


def _hx_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"hx-request", b"true")],
            "query_string": b"",
        }
    )


# ── Endpoint (HTMX): returns immediately, schedules the waterfall ────────────────────


@pytest.mark.asyncio
async def test_enrich_button_schedules_background_and_does_not_run_inline(db_session, test_user, monkeypatch):
    """The HTMX handler registers a background task and returns WITHOUT awaiting the
    waterfall.

    Proven by: enrich_entity / find_suggested_contacts_with_errors are not called during
    the handler, a task IS queued on the BackgroundTasks object, and the account is left in
    the in-flight state with the polling panel returned.
    """
    from app.routers.crm import enrichment as enr

    test_user.role = "manager"  # passes can_manage_account regardless of ownership
    db_session.commit()

    enrich_entity_mock = AsyncMock(return_value={"industry": "Electronics"})
    find_mock = AsyncMock(return_value=([], []))
    monkeypatch.setattr("app.enrichment_service.enrich_entity", enrich_entity_mock)
    monkeypatch.setattr("app.enrichment_service.find_suggested_contacts_with_errors", find_mock)

    # Neutralise the runner so this test only inspects scheduling (no real session opened).
    monkeypatch.setattr(enr, "_run_company_enrichment", AsyncMock())

    from app.models import Company

    company = Company(name="Async Co", domain="async.com", is_active=True)
    db_session.add(company)
    db_session.commit()
    db_session.refresh(company)

    bg = BackgroundTasks()
    resp = await enr.enrich_company(company.id, _hx_request(), bg, EnrichDomainRequest(), test_user, db_session)

    # Waterfall NOT awaited inline.
    enrich_entity_mock.assert_not_called()
    find_mock.assert_not_called()
    # A background task WAS registered, and the account is marked in-flight.
    assert len(bg.tasks) == 1
    assert company_enrich_runs.is_running(company.id) is True
    # The immediate response is the polling "Enriching…" panel.
    body = resp.body.decode()
    assert "Enriching" in body
    assert "every 2s" in body


def test_enrich_button_returns_enriching_panel_over_http(client, db_session, test_user, monkeypatch):
    """Via HTTP: the Enrich button POST returns the polling panel and does not run the
    waterfall inline (the runner is neutralised)."""
    from app.routers.crm import enrichment as enr

    enrich_entity_mock = AsyncMock(return_value={})
    monkeypatch.setattr("app.enrichment_service.enrich_entity", enrich_entity_mock)
    monkeypatch.setattr(enr, "_run_company_enrichment", AsyncMock())

    from app.models import Company

    company = Company(name="Panel Co", domain="panel.com", is_active=True, account_owner_id=test_user.id)
    db_session.add(company)
    db_session.commit()
    db_session.refresh(company)

    resp = client.post(f"/api/enrich/company/{company.id}", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert "Enriching" in resp.text
    assert "every 2s" in resp.text  # poller active
    assert f"/api/enrich/company/{company.id}/status" in resp.text
    enrich_entity_mock.assert_not_called()  # scheduled, not awaited inline
    assert company_enrich_runs.is_running(company.id) is True


def test_enrich_button_double_click_does_not_stack_second_run(client, db_session, test_user, monkeypatch):
    """A run already in flight must not enqueue a second background waterfall."""
    from app.routers.crm import enrichment as enr

    runner = AsyncMock()
    monkeypatch.setattr(enr, "_run_company_enrichment", runner)

    from app.models import Company

    company = Company(name="Dbl Co", domain="dbl.com", is_active=True, account_owner_id=test_user.id)
    db_session.add(company)
    db_session.commit()
    db_session.refresh(company)

    # Simulate a run already in flight.
    assert company_enrich_runs.begin(company.id) is True

    resp = client.post(f"/api/enrich/company/{company.id}", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "Enriching" in resp.text
    runner.assert_not_called()  # no second run scheduled


# ── Background runner: outcome recorded for the poller, own session, never raises ────


@pytest.mark.asyncio
async def test_background_run_records_success_outcome(monkeypatch):
    from app.routers.crm.enrichment import _run_company_enrichment

    fake_session = MagicMock()
    monkeypatch.setattr("app.database.SessionLocal", lambda: fake_session)
    monkeypatch.setattr("app.enrichment_service.enrich_entity", AsyncMock(return_value={"industry": "Electronics"}))
    monkeypatch.setattr("app.enrichment_service.apply_enrichment_to_company", lambda c, e: ["industry"])
    contact = {"full_name": "Jane Buyer", "title": "Buyer", "email": "jane@acme.com", "source": "hunter"}
    monkeypatch.setattr(
        "app.enrichment_service.find_suggested_contacts_with_errors",
        AsyncMock(return_value=([contact], [])),
    )

    company_enrich_runs.begin(4242)
    await _run_company_enrichment(4242, "acme.com", "Acme")

    outcome = company_enrich_runs.consume_outcome(4242)
    assert outcome is not None
    assert outcome.blocked is False
    assert outcome.updated_fields == ["industry"]
    assert outcome.suggested == [contact]
    assert outcome.errored_providers == []
    fake_session.close.assert_called_once()


@pytest.mark.asyncio
async def test_background_run_firmographics_outage_marks_blocked(monkeypatch):
    """enrich_entity raising (data source down) → blocked=True (poller shows the
    toast)."""
    from app.routers.crm.enrichment import _run_company_enrichment

    monkeypatch.setattr("app.database.SessionLocal", lambda: MagicMock())
    monkeypatch.setattr("app.enrichment_service.enrich_entity", AsyncMock(side_effect=RuntimeError("SAM down")))
    monkeypatch.setattr(
        "app.enrichment_service.find_suggested_contacts_with_errors",
        AsyncMock(return_value=([], [])),
    )

    company_enrich_runs.begin(4243)
    await _run_company_enrichment(4243, "acme.com", "Acme")  # must not raise

    outcome = company_enrich_runs.consume_outcome(4243)
    assert outcome is not None
    assert outcome.blocked is True


@pytest.mark.asyncio
async def test_background_run_contact_discovery_failure_degrades_not_blocked(monkeypatch):
    """Firmographics OK but contact discovery raises → NOT blocked; degrades to the
    amber 'couldn't reach' banner via errored_providers (mirrors old inline
    behavior)."""
    from app.routers.crm.enrichment import _run_company_enrichment

    monkeypatch.setattr("app.database.SessionLocal", lambda: MagicMock())
    monkeypatch.setattr("app.enrichment_service.enrich_entity", AsyncMock(return_value={"industry": "Electronics"}))
    monkeypatch.setattr("app.enrichment_service.apply_enrichment_to_company", lambda c, e: ["industry"])
    monkeypatch.setattr(
        "app.enrichment_service.find_suggested_contacts_with_errors",
        AsyncMock(side_effect=RuntimeError("clay down")),
    )

    company_enrich_runs.begin(4244)
    await _run_company_enrichment(4244, "acme.com", "Acme")  # must not raise

    outcome = company_enrich_runs.consume_outcome(4244)
    assert outcome is not None
    assert outcome.blocked is False
    assert outcome.updated_fields == ["industry"]
    assert outcome.errored_providers == ["all"]


@pytest.mark.asyncio
async def test_background_run_company_gone_clears_guard(monkeypatch):
    """If the company vanished between click and run, the guard is dropped and no
    outcome is recorded (nothing to render)."""
    from app.routers.crm.enrichment import _run_company_enrichment

    fake_session = MagicMock()
    fake_session.get.return_value = None
    monkeypatch.setattr("app.database.SessionLocal", lambda: fake_session)

    company_enrich_runs.begin(4245)
    await _run_company_enrichment(4245, "acme.com", "Acme")

    assert company_enrich_runs.is_running(4245) is False
    assert company_enrich_runs.consume_outcome(4245) is None


# ── Poller: in-progress keeps polling, terminal renders panel, blocked toasts ────────


def test_poller_in_progress_keeps_polling(client, db_session, test_user):
    from app.models import Company

    company = Company(name="Poll Co", domain="poll.com", is_active=True, account_owner_id=test_user.id)
    db_session.add(company)
    db_session.commit()
    db_session.refresh(company)

    company_enrich_runs.begin(company.id)  # running, no outcome yet

    resp = client.get(f"/api/enrich/company/{company.id}/status")
    assert resp.status_code == 200
    assert "Enriching" in resp.text
    assert "every 2s" in resp.text
    assert "HX-Trigger" not in resp.headers


def test_poller_terminal_renders_result_panel_and_stops(client, db_session, test_user):
    """On completion the poller returns the firmographics + contacts panel and stops
    polling (286); the outcome is consumed exactly once."""
    from app.models import Company

    company = Company(
        name="Acme Electronics",
        domain="acme.com",
        legal_name="Acme Electronics Inc",
        industry="Electronic Components",
        is_active=True,
        account_owner_id=test_user.id,
    )
    db_session.add(company)
    db_session.commit()
    db_session.refresh(company)

    contact = {"full_name": "Jane Buyer", "title": "Procurement Mgr", "email": "jane@acme.com", "source": "hunter"}
    company_enrich_runs.finish(
        company.id,
        CompanyEnrichOutcome(blocked=False, updated_fields=["industry"], suggested=[contact], errored_providers=[]),
    )

    resp = client.get(f"/api/enrich/company/{company.id}/status")
    assert resp.status_code == 286  # htmx stop-polling
    assert "Acme Electronics Inc" in resp.text  # firmographics
    assert "Electronic Components" in resp.text
    assert "Updated" in resp.text  # the updated-field pill
    assert "Jane Buyer" in resp.text  # discovered contact
    assert "suggested-contacts/add" in resp.text
    assert "from_enrich" in resp.text
    assert "every 2s" not in resp.text  # no longer polling
    assert "HX-Trigger" not in resp.headers  # success → no toast
    # Outcome consumed once.
    assert company_enrich_runs.consume_outcome(company.id) is None


def test_poller_source_unavailable_surfaces_toast(client, db_session, test_user):
    """A blocked run (firmographics source down) fires the 'couldn't complete' toast and
    stops polling."""
    from app.models import Company

    company = Company(name="Down Co", domain="down.com", is_active=True, account_owner_id=test_user.id)
    db_session.add(company)
    db_session.commit()
    db_session.refresh(company)

    company_enrich_runs.finish(company.id, CompanyEnrichOutcome(blocked=True))

    resp = client.get(f"/api/enrich/company/{company.id}/status")
    assert resp.status_code == 286
    trigger = resp.headers.get("HX-Trigger", "")
    assert "showToast" in trigger
    assert "couldn't complete" in trigger


def test_poller_contact_degradation_shows_amber_banner_no_toast(client, db_session, test_user):
    """Contact-discovery failure renders firmographics + the amber banner, NOT a
    toast."""
    from app.models import Company

    company = Company(
        name="Amber Co",
        domain="amber.com",
        industry="Electronic Components",
        is_active=True,
        account_owner_id=test_user.id,
    )
    db_session.add(company)
    db_session.commit()
    db_session.refresh(company)

    company_enrich_runs.finish(
        company.id,
        CompanyEnrichOutcome(blocked=False, updated_fields=["industry"], suggested=[], errored_providers=["all"]),
    )

    resp = client.get(f"/api/enrich/company/{company.id}/status")
    assert resp.status_code == 286
    assert "Electronic Components" in resp.text  # firmographics still render
    assert "Couldn" in resp.text  # amber "Couldn't reach" banner
    assert "HX-Trigger" not in resp.headers  # not blocked → no toast


def test_poller_no_updates_shows_already_current(client, db_session, test_user):
    """An already-enriched account (empty updated_fields) shows 'Already up to date'."""
    from app.models import Company

    company = Company(
        name="Current Co",
        domain="current.com",
        industry="Electronic Components",
        is_active=True,
        account_owner_id=test_user.id,
    )
    db_session.add(company)
    db_session.commit()
    db_session.refresh(company)

    company_enrich_runs.finish(
        company.id,
        CompanyEnrichOutcome(blocked=False, updated_fields=[], suggested=[], errored_providers=[]),
    )

    resp = client.get(f"/api/enrich/company/{company.id}/status")
    assert resp.status_code == 286
    assert "Already up to date" in resp.text


def test_poller_result_panel_javascript_uri_not_an_href(client, db_session, test_user):
    """A stored javascript:/data: website must never be emitted as a clickable href in
    the result panel (XSS: HTML-escaping the text does not neutralize a dangerous URL
    scheme)."""
    from app.models import Company

    company = Company(
        name="XSS Co",
        domain="xss.com",
        website="javascript://%0aalert(document.cookie)",
        is_active=True,
        account_owner_id=test_user.id,
    )
    db_session.add(company)
    db_session.commit()
    db_session.refresh(company)

    company_enrich_runs.finish(
        company.id,
        CompanyEnrichOutcome(blocked=False, updated_fields=[], suggested=[], errored_providers=[]),
    )

    resp = client.get(f"/api/enrich/company/{company.id}/status")
    assert resp.status_code == 286
    body = resp.text.lower()
    assert "href='javascript:" not in body
    assert 'href="javascript:' not in body
    assert "href='data:" not in body
    assert 'href="data:' not in body


def test_poller_no_outcome_stops_polling(client, db_session, test_user):
    """No run in flight and no pending outcome (already consumed / lost on restart) →
    stop polling with an empty body."""
    from app.models import Company

    company = Company(name="Empty Co", domain="empty.com", is_active=True, account_owner_id=test_user.id)
    db_session.add(company)
    db_session.commit()
    db_session.refresh(company)

    resp = client.get(f"/api/enrich/company/{company.id}/status")
    assert resp.status_code == 286
    assert resp.text.strip() == ""


def test_poller_deleted_company_stops_polling(client):
    resp = client.get("/api/enrich/company/999999/status")
    assert resp.status_code == 286
    assert resp.text.strip() == ""


# ── JSON path unchanged: still synchronous ──────────────────────────────────────────


def test_json_path_still_synchronous(client, db_session, test_user, monkeypatch):
    """A programmatic (non-HX) POST still awaits the firmographics inline and returns
    JSON."""
    enrich_entity_mock = AsyncMock(return_value={"industry": "Electronics"})
    monkeypatch.setattr("app.enrichment_service.enrich_entity", enrich_entity_mock)
    monkeypatch.setattr("app.enrichment_service.apply_enrichment_to_company", lambda c, e: ["industry"])

    from app.models import Company

    company = Company(name="Json Co", domain="json.com", is_active=True, account_owner_id=test_user.id)
    db_session.add(company)
    db_session.commit()
    db_session.refresh(company)

    resp = client.post(f"/api/enrich/company/{company.id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["updated_fields"] == ["industry"]
    enrich_entity_mock.assert_awaited_once()  # awaited inline for JSON callers
